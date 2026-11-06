import re
from unicodedata import normalize


_TRIGGERS = (
    ('time_place', ('什么时候', '什么时间', '哪一天', '哪天', '几号', '何时', '在哪里', '在哪', '什么地点', '地点',
                    'when', 'where', 'date', 'location')),
    ('evidence', ('有什么依据', '用什么证据', '事实', '依据', '数据', '证据', '多少',
                  'evidence', 'facts', 'data', 'how many', 'how much')),
    ('terms', ('是什么意思', '什么意思', '怎么定义', '专业词汇', '术语', '定义',
               'definition', 'terminology', 'what does', 'meaning')),
    ('wording', ('怎么说', '怎么写的', '什么措辞', '用词', '措辞', '原话',
                 'wording', 'phrasing', 'exact words', 'how was it worded')),
    ('conclusion', ('得出什么结论', '结论是什么', '结论', '最终结果',
                     'conclusion', 'verdict', 'final result')),
    ('viewpoint', ('持什么观点', '怎么看', '什么看法', '观点', '主张', '认为',
                    'viewpoint', 'opinion', 'perspective', 'what do they think')),
    ('reasoning', ('怎么推出', '推导过程', '什么逻辑', '为什么', '怎么证明', '证明', '推理',
                    'why', 'reasoning', 'derive', 'prove', 'how was it inferred')),
    ('sentiment', ('什么态度', '什么感受', '情绪', '态度', '感受', '感情',
                    'sentiment', 'feelings', 'emotion', 'attitude', 'how did they feel')),
)


def _text(value):
    return normalize('NFKC', value).casefold().strip()


def _match(question, trigger):
    trigger = _text(trigger)
    if not trigger:
        return 0
    if trigger.isascii():
        return len(trigger) if re.search(r'(?<!\w)' + re.escape(trigger) + r'(?!\w)', question) else 0
    return len(trigger) if trigger in question else 0


def literal_fields(question, fields):
    question = _text(question)
    matches = []
    for field in fields:
        value = _text(field['value'])
        if not value:
            continue
        pattern = re.escape(value)
        if value[0].isascii() and value[0].isalnum():
            pattern = r'(?<![a-z0-9_])' + pattern
        if value[-1].isascii() and value[-1].isalnum():
            pattern += r'(?![a-z0-9_])'
        if re.search(pattern, question):
            matches.append(field)
    return tuple(matches)


def section_matches(angles, sections, windows):
    return any(section['angle_id'] in angles and section['start'] <= window.start < window.end <= section['end']
               for section in sections for window in windows)


def v1(question, learned=()):
    question = _text(question)
    matches = []
    for identity, triggers in _TRIGGERS:
        strength = max(_match(question, trigger) for trigger in triggers)
        if strength:
            matches.append((-strength, 1, identity))
    for angle in learned:
        strength = max((_match(question, trigger) for trigger in angle['triggers']), default=0)
        if strength:
            matches.append((-strength, 0, angle['id']))
    return tuple(dict.fromkeys(identity for _, _, identity in sorted(matches)))[:3]
