import importlib
from copy import deepcopy

import pytest

from backend.memory_app.v2.budget import text_tokens


def rule(identity, content, *, strength=0, conditions=()):
    return {'id': identity, 'revision': 1, 'content': content,
            'conditions': list(conditions), 'strength': strength,
            'text': content + (('（适用：' + '；'.join(conditions) + '）') if conditions else '')}


def choose(rows):
    return importlib.import_module('backend.memory_app.v2.policies.style').v1(rows, estimate_tokens=text_tokens)


def test_writing_rules_use_content_or_conditions_and_do_not_select_topic_facts():
    rows = [rule('a', '开头先说明具体结果。'), rule('b', '使用三列展示比较。', conditions=('写表格时',)),
            rule('c', '文档标题为设备检查。'), rule('d', '每周周一检查设备。')]
    result = choose(rows)
    assert result['selected'] == [{'id': 'a', 'revision': 1}, {'id': 'b', 'revision': 1}]
    assert result['count'] == 2
    assert '适用：写表格时' in result['text']
    assert '设备' not in result['text']


def test_same_detached_revisions_and_strengths_are_stable_and_inputs_unchanged():
    rows = [rule('b', '语气保持自然。', strength=2), rule('a', '标题要简洁。', strength=2),
            rule('c', '段落先给结论。', strength=5)]
    before = deepcopy(rows)
    left, right = choose(rows), choose(list(reversed(rows)))
    assert left == right
    assert left['selected'] == [{'id': key, 'revision': 1} for key in ('c', 'a', 'b')]
    assert rows == before


def test_empty_styles_have_no_prefix_and_budget_preserves_atomic_rules():
    assert choose([]) == {'text': '', 'tokens': 0, 'count': 0, 'selected': []}
    rows = [rule(str(index), '段落要简洁。' + '正文' * 22, strength=20 - index) for index in range(20)]
    result = choose(rows)
    assert 0 < result['count'] < len(rows)
    assert result['tokens'] == text_tokens(result['text']) <= 400
    assert result['text'].count('段落要简洁。') == result['count']
    assert '正文' * 22 in result['text']


def test_invalid_strength_or_duplicate_identity_does_not_silently_pick_a_rule():
    with pytest.raises(ValueError, match='strength'):
        choose([rule('a', '标题要简洁。', strength=float('nan'))])
    with pytest.raises(ValueError, match='duplicate'):
        choose([rule('a', '标题要简洁。'), rule('a', '开头先给结果。')])


def test_writing_directive_in_applicability_condition_is_not_lost():
    result = choose([rule('a', '每段一个重点。', conditions=('写作时优先用短句。',))])
    assert result['selected'] == [{'id': 'a', 'revision': 1}]
    assert '适用：写作时优先用短句。' in result['text']
