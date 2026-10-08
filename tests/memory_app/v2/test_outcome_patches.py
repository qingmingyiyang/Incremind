import pytest

from backend.memory_app.v2.outcome_patches import OutcomePatchError, apply_patch


def update(path, body):
    return {'kind': 'update', 'path': path, 'body': body}


def add(path, level, title, body):
    return {'kind': 'add', 'after_path': path, 'level': level, 'title': title, 'body': body}


def test_update_owns_only_its_body_and_retains_other_bytes():
    original = '# 手册\r\n\r\n前言\r\n\r\n## 配置 ##\r\n\r\n旧说明\r\n\r\n### 子项\r\n\r\n原子项\r\n\r\n## 运行\r\n\r\n保留\r\n'
    result = apply_patch(original, original, [update(['手册', '配置'], '新说明')])
    assert result == {'markdown': original.replace('旧说明', '新说明'),
                      'changes': [{'path': ['手册', '配置'], 'kind': 'updated'}]}
    assert original.count('旧说明') == 1


def test_add_sibling_after_the_complete_subtree():
    original = '# 手册\n\n## 配置\n\n说明\n\n### 子项\n\n子项原文\n\n## 运行\n\n运行原文\n'
    result = apply_patch(original, original, [add(['手册', '配置'], 2, '检查', '核对')])
    assert result['markdown'] == original.replace('## 运行', '## 检查\n\n核对\n\n## 运行')
    assert result['changes'] == [{'path': ['手册', '检查'], 'kind': 'added'}]


def test_add_child_at_end_and_allow_later_update_in_the_same_patch():
    original = '# 手册\n\n## 配置\n\n说明\n'
    result = apply_patch(original, original, [add(['手册', '配置'], 3, '检查', '核对'),
                                             update(['手册', '配置', '检查'], '核对新增项')])
    assert result['markdown'] == original + '\n### 检查\n\n核对新增项\n'
    assert result['changes'] == [{'path': ['手册', '配置', '检查'], 'kind': 'added'}]


def test_fenced_fake_headings_are_content_and_cannot_be_targets():
    original = '# 手册\n\n## 配置\n\n```md\n## 假标题\n\n原代码\n```\n\n## 运行\n\n原文\n'
    result = apply_patch(original, original, [update(['手册', '运行'], '新原文')])
    assert result['markdown'] == original.replace('原文', '新原文')
    with pytest.raises(OutcomePatchError, match='path_missing'):
        apply_patch(original, original, [update(['手册', '假标题'], '覆盖')])


def test_user_changed_paragraph_is_retained_while_unchanged_paragraph_can_update():
    birth = '# 手册\n\n## 配置\n\n未修改段。\n\nAI 原段。\n\n另一段。\n'
    current = birth.replace('AI 原段。', '用户写的准确段。')
    result = apply_patch(current, birth, [update(['手册', '配置'],
        '更新未修改段。\n\n用户写的准确段。\n追加说明。\n\n另一段。')])
    assert result['markdown'] == current.replace('未修改段。', '更新未修改段。').replace(
        '用户写的准确段。', '用户写的准确段。\n追加说明。')
    with pytest.raises(OutcomePatchError, match='user_edit_protected'):
        apply_patch(current, birth, [update(['手册', '配置'], '未修改段。\n\n用户写的段。\n\n另一段。')])


@pytest.mark.parametrize('before,changed,append', [
    ('- AI 一\n- AI 二', '- 用户一\n- AI 二', '\n- 新项目'),
    ('| 项目 | 值 |\n| --- | --- |\n| A | 1 |', '| 项目 | 值 |\n| --- | --- |\n| A | 2 |', '\n| B | 3 |'),
    ('```python\nvalue = 1\n\nprint(value)\n```', '```python\nvalue = 2\n\nprint(value)\n```', '\n\n新增说明'),
])
def test_user_lists_tables_and_fences_are_atomic(before, changed, append):
    birth = '# 手册\n\n## 配置\n\n' + before + '\n'
    current = birth.replace(before, changed)
    result = apply_patch(current, birth, [update(['手册', '配置'], changed + append)])
    assert result['markdown'] == current[:-1] + append + '\n'
    with pytest.raises(OutcomePatchError, match='user_edit_protected'):
        apply_patch(current, birth, [update(['手册', '配置'], before)])


def test_full_document_difference_protects_new_user_section_and_preamble():
    birth = '# 手册\n\nAI 前言\n\n## 配置\n\n相同正文\n'
    current = '# 手册\n\n用户前言\n\n## 用户新增标题\n\n相同正文\n'
    with pytest.raises(OutcomePatchError, match='user_edit_protected'):
        apply_patch(current, birth, [update(['手册', '用户新增标题'], '替换')])
    with pytest.raises(OutcomePatchError, match='user_edit_protected'):
        apply_patch(current, birth, [update(['手册'], '替换前言')])


def test_changed_paragraph_cannot_be_moved_to_a_different_section():
    birth = '# 手册\n\n## 甲\n\n原段\n\n## 乙\n\n其他\n'
    current = birth.replace('原段', '用户段')
    with pytest.raises(OutcomePatchError, match='user_edit_protected'):
        apply_patch(current, birth, [update(['手册', '甲'], ''), update(['手册', '乙'], '用户段')])


def test_repeat_user_paragraphs_cannot_be_collapsed_into_one():
    birth = '# 手册\n\n## 配置\n\nAI 原文\n'
    current = '# 手册\n\n## 配置\n\n用户段\n\n用户段\n'
    with pytest.raises(OutcomePatchError, match='user_edit_protected'):
        apply_patch(current, birth, [update(['手册', '配置'], '用户段')])
    assert apply_patch(current, birth, []) == {'markdown': current, 'changes': []}


@pytest.mark.parametrize('body', ['## 注入', '新标题\n---', '<h2>注入</h2>'])
def test_body_cannot_inject_rendered_headings(body):
    original = '# 手册\n\n## 配置\n\n原文\n'
    with pytest.raises(OutcomePatchError, match='body_heading'):
        apply_patch(original, original, [update(['手册', '配置'], body)])


def test_body_can_quote_pseudo_headings_inside_fences():
    original = '# 手册\n\n## 配置\n\n原文\n'
    body = '~~~markdown\n## 伪标题\n标题\n---\n<h2>示例</h2>\n~~~'
    assert apply_patch(original, original, [update(['手册', '配置'], body)])['markdown'] == original.replace('原文', body)


@pytest.mark.parametrize('operation', [
    {'kind': 'delete', 'path': ['手册']},
    {'kind': 'update', 'path': ['手册'], 'body': '正文', 'title': '改名'},
    {'kind': 'update', 'path': '手册', 'body': '正文'},
    {'kind': 'update', 'path': ['手册'], 'body': None},
    {'kind': 'add', 'after_path': ['手册'], 'level': True, 'title': '新增', 'body': '正文'},
    {'kind': 'add', 'after_path': ['手册'], 'level': 3, 'title': '跳级', 'body': '正文'},
    {'kind': 'add', 'after_path': ['手册'], 'level': 2, 'title': '新增\n## 注入', 'body': '正文'},
])
def test_invalid_operations_raise_structured_error(operation):
    original = '# 手册\n\n## 配置\n\n原文\n'
    with pytest.raises(OutcomePatchError) as failed:
        apply_patch(original, original, [operation])
    assert failed.value.operation_index == 0
    assert isinstance(failed.value.code, str) and failed.value.code


def test_duplicate_paths_and_added_collisions_are_rejected():
    original = '# 手册\n\n## 配置\n\n一\n\n## 配置\n\n二\n'
    with pytest.raises(OutcomePatchError, match='path_ambiguous'):
        apply_patch(original, original, [update(['手册', '配置'], '正文')])
    original = '# 手册\n\n## 配置\n\n一\n'
    with pytest.raises(OutcomePatchError, match='path_ambiguous'):
        apply_patch(original, original, [add(['手册', '配置'], 2, '配置', '正文')])


def test_noop_preserves_exact_bytes_and_has_no_change_markers():
    original = '开头\n\n# 手册\n\n## 配置\n\n原文\n\n'
    assert apply_patch(original, original, [update(['手册', '配置'], '原文')]) == {
        'markdown': original, 'changes': []}


def test_unclosed_fence_is_rejected_before_it_can_swallow_old_headings():
    original = '# 手册\n\n## 配置\n\n原文\n\n## 运行\n\n另文\n'
    with pytest.raises(OutcomePatchError, match='fence_unclosed'):
        apply_patch(original, original, [update(['手册', '配置'], '```\n正文')])


def test_user_inline_suffix_requires_a_line_boundary():
    birth = '# 手册\n\n## 配置\n\n原句\n'
    current = birth.replace('原句', '用户句')
    with pytest.raises(OutcomePatchError, match='user_edit_protected'):
        apply_patch(current, birth, [update(['手册', '配置'], '用户句被替换意义')])


def test_update_without_a_heading_linebreak_cannot_change_the_heading():
    original = '# 手册'
    assert apply_patch(original, original, [update(['手册'], '补充')])['markdown'] == '# 手册\n补充'


def test_add_after_final_fenced_code_keeps_existing_text_exact():
    original = '# 手册\n\n## 配置\n\n```\n代码\n```'
    result = apply_patch(original, original, [add(['手册', '配置'], 2, '运行', '说明')])
    assert result['markdown'] == original + '\n\n## 运行\n\n说明\n'


@pytest.mark.parametrize('blank', ['', '\n'])
def test_empty_own_body_can_receive_text_without_swallowing_a_child(blank):
    original = '# 手册\n\n## 配置\n' + blank + '### 子项\n\n子项原文\n'
    result = apply_patch(original, original, [update(['手册', '配置'], '新增说明')])
    assert result['markdown'] == '# 手册\n\n## 配置\n\n新增说明\n\n### 子项\n\n子项原文\n'


def test_identical_user_added_paragraph_cannot_be_lost_by_matching_the_ai_copy():
    birth = '# 手册\n\n## 配置\n\n同一句话\n'
    current = birth + '\n同一句话\n'
    with pytest.raises(OutcomePatchError, match='user_edit_protected'):
        apply_patch(current, birth, [update(['手册', '配置'], '同一句话')])
    assert apply_patch(current, birth, [update(['手册', '配置'], '同一句话\n\n同一句话\n追加')])[
        'markdown'] == current[:-1] + '\n追加\n'
