from copy import deepcopy

import pytest

from backend.memory_app.v2.policies.route_correction import v2


def source(index, intent, dependencies=(), *, project='default', items=()):
    return {'turn_id': f'turn-{index}', 'intent': intent, 'span': f'部分{index}',
        'depends_on': list(dependencies), 'item_ids': list(items),
        'source_members': [{'turn_id': f'turn-{index}', 'intent': intent}],
        'scope': {'project_id': project, 'scene': None, 'namespace': 'personal/local-user/default',
            'thread_id': f'thread-{project}'}}


def change(sources, intent, dependencies=(), *, target=None):
    return {'sources': sources, 'intent': intent, 'depends_on': list(dependencies),
        'target_source': sources[0] if target is None else target}


def test_merge_reexecutes_changed_part_and_its_downstream_without_losing_sources():
    parts = [source(0, 'remember', items=('file-a',)),
        source(1, 'ask', (0,), items=('file-b',)), source(2, 'do', (1,))]
    before = deepcopy(parts)
    result = v2(parts, [change([0, 1], 'remember'), change([2], 'do', (0,))])
    assert result['affected'] == [0, 1]
    assert result['deleted'] == []
    assert result['parts'][0]['span'] == '部分0\n部分1'
    assert result['parts'][0]['item_ids'] == ['file-a', 'file-b']
    assert [member['turn_id'] for member in result['parts'][0]['source_members']] == ['turn-0', 'turn-1']
    assert parts == before


def test_deleting_unrelated_part_reuses_unmodified_results_and_remaps_dependencies():
    parts = [source(0, 'remember'), source(1, 'remember'), source(2, 'do', (0,))]
    result = v2(parts, [change([0], 'remember'), change([2], 'do', (0,))])
    assert result['deleted'] == [1]
    assert result['affected'] == []
    assert [part['turn_id'] for part in result['parts']] == ['turn-0', 'turn-2']
    assert v2(parts, []) == {'parts': [], 'affected': [], 'deleted': [0, 1, 2]}


def test_merge_uses_explicit_original_target_and_preserves_attachment_members():
    parts = [source(0, 'remember', items=('file-a',)),
        source(1, 'remember', project='other', items=('file-b',))]
    result = v2(parts, [change([0, 1], 'remember', target=1)])
    merged = result['parts'][0]
    assert merged['target_scope'] == parts[1]['scope']
    assert merged['attachment_aggregate'] is True
    assert result['affected'] == [0]
    with pytest.raises(ValueError, match='target_invalid'):
        v2(parts, [change([0], 'remember', target=1)])


def test_reordering_duplicate_sources_and_invalid_dependency_directions_are_rejected():
    parts = [source(0, 'remember'), source(1, 'ask', (0,))]
    with pytest.raises(ValueError, match='order_changed'):
        v2(parts, [change([1], 'ask'), change([0], 'remember')])
    with pytest.raises(ValueError, match='order_changed'):
        v2(parts, [change([0], 'remember'), change([0], 'ask')])
    with pytest.raises(ValueError, match='dependency_direction'):
        v2(parts, [change([0], 'ask'), change([1], 'ask', (0,))])


def test_examples_prefer_same_project_then_corrections_and_keep_at_most_three():
    examples = [
        {'id': 'other-corrected', 'same_project': False, 'corrected': True, 'score': 1},
        {'id': 'same-ordinary', 'same_project': True, 'corrected': False, 'score': 1},
        {'id': 'same-corrected-low', 'same_project': True, 'corrected': True, 'score': .3},
        {'id': 'same-corrected-high', 'same_project': True, 'corrected': True, 'score': .8},
        {'id': 'other-ordinary', 'same_project': False, 'corrected': False, 'score': 1},
    ]
    assert [row['id'] for row in v2.rank_examples(examples)] == [
        'same-corrected-high', 'same-corrected-low', 'same-ordinary']
