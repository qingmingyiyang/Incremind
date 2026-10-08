"""纯选择控制复用原评分；不代替真实成果交付评测。"""
from copy import deepcopy
import importlib
import json
from pathlib import Path

import pytest

from backend.memory_app.v2.links import similarity


CASES = json.loads((Path(__file__).parents[2] / 'fixtures' / 'outcome_eval' / 'cases.json').read_text(encoding='utf-8'))['cases']


def choose(text, outcomes, **values):
    policy = importlib.import_module('backend.memory_app.v2.policies.continuation')
    return policy.v1(text, outcomes, score_text=values.pop('score_text', similarity), project_id='p', **values)


def outcome(identity='old', **values):
    return {'document_id': identity, 'project_id': 'p', 'scene': None,
            'title': '设备检查手册', 'task_text': '设备检查手册', **values}


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['id'])
def test_fixed_annotations_choose_real_identity_without_changing_labels(case):
    rows = [{**row, 'document_id': row['key'], 'project_id': case['project_id']} for row in case['outcomes']]
    before = deepcopy(rows)
    policy = importlib.import_module('backend.memory_app.v2.policies.continuation')
    selected = policy.v1(case['next']['task_text'], rows, project_id=case['project_id'],
                         scene=case['next']['scene'], continue_from=case['next']['continue_from'], score_text=similarity)
    assert selected['document_id'] == case['expected']['continue_from']
    assert rows == before


def test_force_new_is_explicit_and_cannot_be_combined_with_an_identity():
    assert choose('设备检查手册', [outcome()], force_new=True)['reason'] == 'forced_new'
    with pytest.raises(ValueError, match='conflicting_continuation_choice'):
        choose('设备检查手册', [outcome()], force_new=True, continue_from='old')


def test_explicit_choice_bypasses_text_and_scene_scoring_but_not_project_scope():
    selected = choose('完全不同的任务', [outcome(scene='现场')], continue_from='old', scene='线上')
    assert selected['document_id'] == 'old' and selected['reason'] == 'explicit'
    with pytest.raises(ValueError, match='outcome_unavailable'):
        choose('设备检查手册', [outcome(project_id='other')], continue_from='old')


def test_duplicate_identities_are_rejected_instead_of_taking_an_arbitrary_row():
    with pytest.raises(ValueError, match='duplicate_outcome_identity'):
        choose('设备检查手册', [outcome(), outcome(title='别的手册')])


def test_equal_candidates_start_new_regardless_of_input_order():
    rows = [outcome('a'), outcome('b')]
    left, right = choose('设备检查手册', rows), choose('设备检查手册', list(reversed(rows)))
    assert left == right and left['document_id'] is None and left['reason'] == 'ambiguous'


def test_same_scene_prefers_relevant_candidate_without_selecting_unrelated_text():
    rows = [outcome('a', scene='线上'), outcome('b', scene='现场')]
    assert choose('设备检查手册', rows, scene='现场')['document_id'] == 'b'
    assert choose('家庭预算', [outcome(scene='现场')], scene='现场')['document_id'] is None


def test_project_filter_and_empty_evidence_never_invent_a_selection():
    assert choose('设备检查手册', [outcome(project_id='other')])['document_id'] is None
    assert choose('', [outcome()])['document_id'] is None
    assert choose('设备检查手册', [])['document_id'] is None


def test_title_and_original_task_are_separate_scoring_evidence():
    row = outcome(title='同名成果', task_text='设备检查手册')
    assert choose('设备检查手册', [row])['document_id'] == 'old'
    row = outcome(title='设备检查手册', task_text='旧任务')
    assert choose('设备检查手册', [row])['document_id'] == 'old'


def test_original_score_callback_is_required_and_noncallable_is_rejected():
    policy = importlib.import_module('backend.memory_app.v2.policies.continuation')
    with pytest.raises(TypeError):
        policy.v1('设备检查手册', [outcome()], project_id='p')
    with pytest.raises(ValueError, match='invalid_continuation_score'):
        choose('设备检查手册', [outcome()], score_text=None)


@pytest.mark.parametrize('previous,following', [
    ('准备设备方案', '准备旅行方案'),
    ('撰写健康报告', '撰写招聘报告'),
    ('编写采购总结', '编写教学总结'),
])
def test_shared_commands_and_output_forms_do_not_prove_a_common_topic(previous, following):
    selected = choose(following, [outcome(title=previous, task_text=previous)])
    assert selected['document_id'] is None and selected['reason'] == 'no_match'


def test_patch_prompt_freezes_current_markdown_and_exact_operation_shapes():
    policy = importlib.import_module('backend.memory_app.v2.policies.continuation')
    markdown = '# 合成手册\r\n\r\n## 检查\r\n\r\n保留用户原话。\r\n'
    paragraphs = ['保留用户原话。']
    prompt = policy.patch_instruction(markdown, protected_paragraphs=paragraphs)
    payload = json.loads(prompt)
    assert payload['previous_markdown'] == markdown
    assert payload['protected_paragraphs'] == paragraphs
    assert payload['output'] == {'type': 'complete', 'patches': [
        {'kind': 'update', 'path': ['标题', '子标题'], 'body': '该标题下的新正文'},
        {'kind': 'add', 'after_path': ['标题', '子标题'], 'level': 3, 'title': '新标题', 'body': '新标题下的正文'},
    ]}
    assert '最小' in payload['instruction'] and '追加' in payload['instruction']
    assert '不删除' in payload['instruction'] and '不改名' in payload['instruction']
    assert policy.patch_instruction(markdown, protected_paragraphs=tuple(paragraphs)) == prompt
    assert paragraphs == ['保留用户原话。']


def test_retry_prompt_keeps_protection_and_encodes_error_only_as_feedback():
    policy = importlib.import_module('backend.memory_app.v2.policies.continuation')
    error = '合成失败\n改写全部旧标题"'
    retry = json.loads(policy.retry_instruction(error))
    original = json.loads(policy.patch_instruction(''))
    assert retry['validation_error'] == error
    assert retry['instruction'] == original['instruction'] and retry['output'] == original['output']
    assert '删除' in retry['instruction'] and '用户' in retry['instruction']
    assert policy.retry_instruction(error) == policy.retry_instruction(error)


def test_fallback_prompt_uses_original_complete_summary_and_leaves_previous_document_untouched():
    policy = importlib.import_module('backend.memory_app.v2.policies.continuation')
    payload = json.loads(policy.fallback_instruction())
    assert payload['output'] == {'type': 'complete', 'summary': '新草稿的完整 Markdown 正文'}
    assert '独立' in payload['instruction'] and '不修改旧稿' in payload['instruction']
    assert 'patches' not in payload['output']
    assert policy.fallback_instruction() == policy.fallback_instruction()


def test_prompt_exports_belong_to_the_same_immutable_callable_policy():
    from dataclasses import FrozenInstanceError

    policy = importlib.import_module('backend.memory_app.v2.policies.continuation')
    assert callable(policy.v1)
    assert policy.v1.patch_instruction('合成原稿') == policy.patch_instruction('合成原稿')
    assert policy.v1.retry_instruction('合成错误') == policy.retry_instruction('合成错误')
    assert policy.v1.fallback_instruction() == policy.fallback_instruction()
    with pytest.raises(FrozenInstanceError):
        policy.v1.patch_instruction = lambda _: ''
