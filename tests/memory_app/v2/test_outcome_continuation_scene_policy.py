"""同场景优先参与资格判断，旧版本与固定标注仍保留。"""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.memory_app.v2.links import similarity
from backend.memory_app.v2.policies import get


CASES = json.loads((Path(__file__).parents[2] / 'fixtures/outcome_eval/cases.json').read_text(encoding='utf-8'))['cases']


def rows_for(case):
    return [{**row, 'document_id': row['key'], 'project_id': case['project_id']}
            for row in case['outcomes']]


def select(version, case, text):
    return get('continuation', version=version)(text, rows_for(case),
        project_id=case['project_id'], scene=case['next']['scene'],
        continue_from=case['next']['continue_from'], score_text=similarity)


def test_materials_dilute_old_score_but_same_scene_preserves_real_task_choice():
    case = next(row for row in CASES if row['id'] == 'scene-preference')
    text = case['next']['task_text']+'\n\n新材料：\n'+'\n'.join(case['next']['new_material'])
    assert select('@1', case, text)['document_id'] is None
    chosen = select('@2', case, text)
    assert chosen['document_id'] == 'field' and chosen['reason'] == 'automatic'
    # 线上候选仍低于阈值，唯一合格候选的领先值沿原规则等于自身排名分。
    assert chosen['score'] == .047619 and chosen['margin'] == .097619


@pytest.mark.parametrize('case', CASES, ids=lambda case:case['id'])
def test_new_version_keeps_all_original_fixed_task_choices(case):
    before = deepcopy(case)
    assert select('@2', case, case['next']['task_text'])['document_id'] == case['expected']['continue_from']
    assert case == before


def test_same_scene_without_any_shared_topic_still_starts_new():
    case = next(row for row in CASES if row['id'] == 'scene-preference')
    chosen = select('@2', case, '家庭预算')
    assert chosen['document_id'] is None and chosen['reason'] == 'no_match'


def test_same_scene_tie_is_order_independent_and_never_picks_arbitrarily():
    case = next(row for row in CASES if row['id'] == 'scene-preference')
    rows = rows_for(case)
    for row in rows:
        row['scene'] = case['next']['scene']
    policy = get('continuation', version='@2')
    values = {'project_id':case['project_id'], 'scene':case['next']['scene'], 'score_text':similarity}
    left = policy(case['next']['task_text'], rows, **values)
    right = policy(case['next']['task_text'], list(reversed(rows)), **values)
    assert left == right and left['document_id'] is None and left['reason'] == 'ambiguous'
