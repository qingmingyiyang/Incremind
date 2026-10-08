from types import SimpleNamespace

import pytest

from backend.memory_app.v2.policies import get
from backend.memory_app.v2.policies.types import ExtractInput
from backend.recognition import normalize_conditions


def decode(output):
    return get('extract', version='@2').decide(SimpleNamespace(
        output=output, normalize_conditions=normalize_conditions,
        neighbors=({'id': 'old', 'text': '已有认识'},),
        projects=({'id': 'alpha'}, {'id': 'beta'}), project_id='alpha'))


def candidate(kind='supplement', relation='supplement', **changes):
    return dict(kind=kind, relation=relation, text='补充方法', conditions=['遇到条件时'],
                target_id='old', scope_hint='beta', **changes)


@pytest.mark.parametrize('kind,relation,target', [
    ('supplement', 'supplement', 'old'), ('differs', 'differs', 'old'),
    ('differs', 'may_supersede', 'old'), ('new_method', 'new', None),
])
def test_three_output_types_and_four_candidate_relations(kind, relation, target):
    row = candidate(kind, relation)
    row['target_id'] = target
    result = decode({'insights': [row], 'supports': []})
    assert result.valid and result.rows == (('补充方法', ('遇到条件时',)),)
    assert result.hints == ({'relation': relation, 'target_id': target, 'scope_hint': 'beta'},)


def test_duplicate_becomes_support_instead_of_candidate():
    result = decode({'insights': [], 'supports': [{'target_id': 'old', 'evidence': '另一份材料印证'}]})
    assert result.valid and result.rows == () and result.hints == ()
    assert result.supports == ({'relation': 'duplicate_of', 'target_id': 'old', 'evidence': '另一份材料印证'},)


def test_unknown_destination_is_cleared_and_method_conditions_are_required():
    row = candidate()
    row['scope_hint'] = 'private-or-unknown'
    result = decode({'insights': [row], 'supports': []})
    assert result.valid and result.hints[0]['scope_hint'] is None
    row.update(kind='new_method', relation='new', target_id=None, conditions=[])
    assert not decode({'insights': [row], 'supports': []}).valid


def test_unknown_neighbor_and_incompatible_relation_are_rejected():
    row = candidate()
    row['target_id'] = 'outside-neighborhood'
    assert not decode({'insights': [row], 'supports': []}).valid
    row.update(target_id='old', relation='new')
    assert not decode({'insights': [row], 'supports': []}).valid


def test_prompt_is_deterministic_and_old_prompt_remains_exact_shape():
    request = SimpleNamespace(experiences=({'id': 'experience', 'content': '材料'},),
        source_constraints='来源约束', neighbors=({'id': 'old', 'text': '已有认识'},),
        projects=({'id': 'alpha', 'name': '项目', 'overview': '首句。'},), project_id='alpha')
    policy = get('extract', version='@2')
    first = policy.prepare(request)
    assert first == policy.prepare(request)
    assert all(word in first[0]['content'] for word in ('supplement', 'differs', 'new_method', 'conditions', '40', '支持', '时间'))
    import json
    assert json.loads(first[1]['content'])['neighbors'][0]['id'] == 'old'
    old = get('extract', version='@1').prepare(ExtractInput(request.experiences, request.source_constraints))
    assert set(json.loads(old[1]['content'])) == {'experiences'}
