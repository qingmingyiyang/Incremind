"""从已核验的认识投影中挑选稳定、限额的写法块。"""
from math import isfinite


MAX_TOKENS = 400
PREFIX = '已确认的写法（只约束表达方式，不作为任务事实）：\n'
WRITING_TERMS = ('写法', '写作', '行文', '措辞', '语气', '篇幅', '标题', '段落',
                 '开头', '结尾', '表格', '列表', '用词', '文风', '结构')
DIRECTIVES = ('优先', '避免', '不要', '采用', '使用', '写成', '列出', '控制',
              '简短', '简洁', '简明', '精炼', '口语', '正式', '先', '保持', '用', '要', '应')


def _writing(row):
    content = row['content']
    conditions = row['conditions']
    evidence = '\n'.join((content, *conditions))
    return (any(word in evidence for word in WRITING_TERMS)
            and any(word in evidence for word in DIRECTIVES))


def v1(rows, *, estimate_tokens):
    """调用方核验确认、遗忘、范围与私密；策略不取得存储或外发权限。"""
    if not callable(estimate_tokens):
        raise ValueError('invalid_style_estimator')
    identities, eligible = set(), []
    for row in rows:
        identity = row['id']
        if not isinstance(identity, str) or not identity:
            raise ValueError('invalid_style_identity')
        if identity in identities:
            raise ValueError('duplicate_style_identity')
        identities.add(identity)
        if type(row['revision']) is not int or row['revision'] < 1:
            raise ValueError('invalid_style_revision')
        if (not isinstance(row['content'], str) or not isinstance(row['text'], str)
                or not isinstance(row['conditions'], (list, tuple))
                or any(not isinstance(value, str) for value in row['conditions'])):
            raise ValueError('invalid_style_content')
        strength = row['strength']
        if type(strength) not in (float, int) or not isfinite(strength) or strength < 0:
            raise ValueError('invalid_style_strength')
        if _writing(row):
            eligible.append(row)
    text, selected, tokens = '', [], 0
    for row in sorted(eligible, key=lambda item: (-item['strength'], item['id'])):
        proposed = (text or PREFIX) + '- ' + row['text'] + '\n'
        size = estimate_tokens(proposed)
        if type(size) is not int or size < 0:
            raise ValueError('invalid_style_estimate')
        # 同画像块保持整条认识及其条件，不截断段落来挤入预算。
        if size > MAX_TOKENS:
            break
        text, tokens = proposed, size
        selected.append({'id': row['id'], 'revision': row['revision']})
    return {'text': text, 'tokens': tokens, 'count': len(selected), 'selected': selected}
