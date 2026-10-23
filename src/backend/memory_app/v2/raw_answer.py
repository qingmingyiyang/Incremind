"""A literal, single-record contract for the opt-in raw-answer-v1 route.

This is extractive relation matching, not general language entailment.  Open
literal subjects/properties use a finite possessive question/statement grammar;
VP inversion, counting, dialogue attribution and implicit qualifiers stay out.
Every nonblank line of each selected *complete* L0 source must be a standalone
declaration.  Conflicts are checked only in those retrieved records.
Values are unit-bearing scalars, single ASCII identifiers, or complete quoted
literal data.  Arbitrary unquoted text is not a proof of absent qualifiers.
"""
from copy import deepcopy
from datetime import date
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from ..structured_generation import AskOutput
from .answer_evidence import build_evidence_catalog


class RawCoverage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    subject: StrictStr
    predicate: StrictStr
    value: StrictStr
    condition: StrictStr
    time: StrictStr


class RawAnswerOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    status: Literal['answer', 'insufficient']
    answer: StrictStr
    unit_id: StrictInt
    quote: StrictStr
    coverage: RawCoverage


_CN_Q = re.compile(r'(?P<subject>.+?)的(?P<predicate>.+?)(?:是(?P<kind>谁|哪里|什么|多少)|为(?P<number>多少))[？?]')
_CN_S = re.compile(r'(?P<subject>.+?)的(?P<predicate>.+?)[是为](?P<value>.+?)[。.]')
_EN_OF_Q = re.compile(r'[Ww]hat is the (?P<predicate>.+?) of (?P<subject>.+?)\?')
_EN_POS_Q = re.compile(r"[Ww]hat is (?P<subject>.+?)(?:'s|’s) (?P<predicate>.+?)\?")
_EN_OF_S = re.compile(r'[Tt]he (?P<predicate>.+?) of (?P<subject>.+?) is (?P<value>.+?)\.')
_EN_POS_S = re.compile(r"(?P<subject>.+?)(?:'s|’s) (?P<predicate>.+?) is (?P<value>.+?)\.")
_CN_ENV = ('开发环境', '测试环境', '生产环境', '预发布环境')
_EN_ENV = ('development', 'testing', 'production', 'staging')
_DATE = r'\d{4}-\d{2}-\d{2}'
_NUMBER = r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?'
_CN_UNITS = ('次', '秒', '毫秒', '分钟', '小时', '天', '周', '月', '年', '个', '人',
             '名', '项', '条', '字节', '字符', '米', '公里', '摄氏度', 'KB', 'MB', 'GB', '%')
_EN_UNITS = ('retry', 'retries', 'attempt', 'attempts', 'second', 'seconds', 'millisecond',
             'milliseconds', 'minute', 'minutes', 'hour', 'hours', 'day', 'days',
             'week', 'weeks', 'byte', 'bytes', 'item', 'items', 'person', 'people',
             'record', 'records', 'character', 'characters')
_SCALAR = re.compile(_NUMBER + r'(?: ?(?:' + '|'.join(_CN_UNITS) + r')| (?:' + '|'.join(_EN_UNITS) + r'))')
_IDENTIFIER = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,99}')
_QUOTES = {'“': '”', '‘': '’', '「': '」', '『': '』', '"': '"', "'": "'"}
# Functional words are grammar exclusions, not entity/property whitelists.
_CN_FUNCTION = (
    '的', '是', '为', '在', '如果', '若', '除非', '因为', '所以', '但是', '虽然',
    '或者', '以及', '和', '与', '并且', '不', '未', '没', '无', '非', '可能', '应该',
    '必须', '将会', '曾经', '当前', '现在', '今天', '昨天', '明天', '今年', '去年',
    '明年', '本周', '上周', '下周', '最近', '过去', '目前', '之前', '之后',
    '最新', '现任', '现行', '实时', '即时', '当下', '当今', '现阶段', '近期', '新近',
    '近来', '近日', '刚刚', '刚才', '此刻', '此时', '至今', '迄今', '截至', '新任',
    '所有', '全部', '任何', '任意', '每个', '各个', '一些', '多个', '哪些',
)
_CN_DEICTIC = {'我', '你', '他', '她', '它', '我们', '你们', '他们', '她们', '它们',
               '自己', '这里', '那里', '这', '那', '这个', '那个', '该项目', '该服务'}
_CN_REFERENCE_PREFIX = ('我', '你', '他', '她', '它', '这', '那', '该', '此', '其', '某')
_EN_FUNCTION = frozenset(('the a an of is are was were be been being and or nor but if unless '
    'because although when where who what which whose how not no never may might could would '
    'should must will can currently current now today yesterday tomorrow this that these those '
    'it its they them their he him his she her we us our you your i me my all any every some '
    'many several most few former formerly previous previously next last recent recently '
    'in on at for from to with without before after during until since').split())
_EN_DEICTIC = frozenset(('here there myself yourself yourselves himself herself itself '
                        'ourselves themselves oneself').split())
_EN_TEMPORAL = frozenset(('latest newest current currently now today yesterday tomorrow '
                         'recent recently previous previously next last former formerly '
                         'present presently updated this').split())


def _numeric_periods(value):
    return all(character != '.' or (0 < index < len(value) - 1
               and value[index - 1].isdigit() and value[index + 1].isdigit())
               for index, character in enumerate(value))


def _value(value):
    """Keep the full lexeme, including any quote delimiters; infer no meaning."""
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        return False
    if value[0] in _QUOTES:
        closing = _QUOTES[value[0]]
        return (len(value) > 2 and value[-1] == closing
                and closing not in value[1:-1] and value[0] not in value[1:-1]
                and not any(character in value for character in '\r\n\t'))
    return bool(_SCALAR.fullmatch(value) or _IDENTIFIER.fullmatch(value)
                and _numeric_periods(value) and value.casefold() not in _EN_FUNCTION | _EN_DEICTIC)


def _literal(value, *, language):
    """Retain literal spelling; never join spaces or infer entity identity."""
    if not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= 100:
        return False
    if not _numeric_periods(value):
        return False
    # Grammar screening also applies to mixed-script slots.  Tokenization is
    # only a rejection check: it never rewrites the literal or the question.
    grammar_words = re.findall(r'[a-z]+', value.casefold())
    if (any(word in _EN_TEMPORAL for word in grammar_words)
            or any(grammar_words[index:index + 3] == ['up', 'to', 'date']
                   for index in range(len(grammar_words) - 2))
            or any(grammar_words[index:index + 2] == ['as', 'of']
                   for index in range(len(grammar_words) - 1))):
        return False
    if language == 'zh':
        if value.startswith(_CN_REFERENCE_PREFIX):
            return False
        if not re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9_%-]+', value):
            return False
        grammar = value.replace('_', '').replace('-', '')
        return value not in _CN_DEICTIC and not any(word in grammar for word in _CN_FUNCTION)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _%.-]*', value) or '  ' in value:
        return False
    words = value.casefold().split(' ')
    if any(word in _EN_FUNCTION for word in words):
        return False
    # Two adjacent numeric tokens are not silently interpreted as one number.
    return not any(a.isdigit() and b.isdigit() for a, b in zip(words, words[1:]))


def _prefix(text, language):
    condition, clock = '', ''
    if language == 'zh':
        match = re.match(r'在(' + '|'.join(_CN_ENV) + r')，', text)
        if match:
            condition, text = match[1], text[match.end():]
        match = re.match(r'在(' + _DATE + r')，', text)
    else:
        match = re.match(r'In (' + '|'.join(_EN_ENV) + r'), ', text)
        if match:
            condition, text = match[1], text[match.end():]
        match = re.match(r'On (' + _DATE + r'), ', text)
    if match:
        clock, text = match[1], text[match.end():]
        try:
            date.fromisoformat(clock)
        except ValueError:
            return None
    return text, condition, clock


def _parse(text, *, question):
    if (not isinstance(text, str) or not 1 <= len(text) <= 400
            or text != text.strip(' \t') or any(c in text for c in '\r\n\t')):
        return None
    for language, patterns in (
        ('zh', (_CN_Q,) if question else (_CN_S,)),
        ('en', (_EN_OF_Q, _EN_POS_Q) if question else (_EN_OF_S, _EN_POS_S)),
    ):
        prefix = _prefix(text, language)
        if prefix is None:
            continue
        body, condition, clock = prefix
        for pattern in patterns:
            match = pattern.fullmatch(body)
            if match is None:
                continue
            slots = {key: match[key] for key in ('subject', 'predicate')}
            if not all(_literal(value, language=language) for value in slots.values()):
                continue
            if not question:
                slots['value'] = match['value']
                if not _value(slots['value']):
                    continue
            return {'language': language, **slots, 'condition': condition, 'time': clock}
    return None


def parse_question(question):
    """Parse the original question, independently of any proposed evidence."""
    return _parse(question, question=True)


def preflight(plan):
    """Return a frozen JSON contract, or None; no model/owner/service access.

    Owner authorization, project scope and capture metadata remain the native
    service guard's responsibility.  This pure contract does not invent trust.
    """
    if not isinstance(plan, dict) or plan.get('history') or plan.get('part_context'):
        return None
    request = parse_question(plan.get('question'))
    if request is None or plan.get('policy_versions', {}).get('compose') not in {'@34', '@35', '@36'}:
        return None
    chosen = plan.get('chosen')
    if not isinstance(chosen, (tuple, list)) or not 1 <= len(chosen) <= 16:
        return None
    bindings = []
    for candidate in chosen:
        if (not isinstance(candidate, dict) or candidate.get('kind') != 'source'
                or candidate.get('layer') != 'L0' or candidate.get('coordinate_space') != 'source_content_v1'):
            return None
        entry, windows = candidate.get('entry'), candidate.get('windows')
        if not isinstance(entry, dict) or not isinstance(entry.get('content'), str):
            return None
        content = entry['content']
        if not content or len(content) > 32000 or not isinstance(windows, (tuple, list)) or len(windows) != 1:
            return None
        window = windows[0]
        if (getattr(window, 'start', None) != 0 or getattr(window, 'end', None) != len(content)
                or getattr(window, 'text', None) != content or candidate.get('excerpt') != content):
            return None
        bindings.append({'source_id': candidate.get('id'), 'entry_id': entry.get('id'),
                         'revision': entry.get('revision'),
                         'sha256': hashlib.sha256(content.encode('utf-8')).hexdigest()})
    try:
        catalog = build_evidence_catalog(chosen, compose_version=plan['policy_versions']['compose'])
    except (ValueError, TypeError, KeyError):
        return None
    if len(catalog['units']) > 256:
        return None
    records, matching = [], []
    for unit in catalog['units']:
        quote = unit['text'].rstrip('\r\n')
        if not quote.strip():
            continue
        declaration = _parse(quote, question=False)
        # An unmatched heading, dialogue turn or qualifier makes context unknown.
        if declaration is None:
            return None
        record = {'unit_id': unit['id'], 'source_number': unit['source_number'],
                  'quote': quote, **declaration}
        records.append(record)
        if all(declaration[key] == request[key] for key in ('language', 'subject', 'predicate', 'condition', 'time')):
            matching.append(record)
    if not matching or len({record['value'] for record in matching}) != 1:
        return None
    expected_value = matching[0]['value']
    for record in records:
        if not all(record[key] == request[key] for key in ('language', 'subject', 'predicate')):
            continue
        # Do not answer an unqualified question while a retrieved exception or
        # explicit date shows that applicability is a missing required slot.
        if any(not request[key] and record[key] for key in ('condition', 'time')):
            return None
        # A general declaration remains relevant to an explicitly qualified
        # question; a different explicit environment/date is a different slot.
        compatible = all(not record[key] or record[key] == request[key] for key in ('condition', 'time'))
        if compatible and record['value'] != expected_value:
            return None
    contract = {'version': 'raw-answer-v1', 'scope': 'retrieved_evidence_only',
                'question': plan['question'], **request, 'records': matching,
                'retrieved_records': records, 'sources': bindings, 'evidence_catalog': catalog}
    # All replay input must survive an ordinary JSON roundtrip without coercion.
    try:
        return contract if json.loads(json.dumps(contract, ensure_ascii=False)) == contract else None
    except (TypeError, ValueError):
        return None


def _current(plan, contract):
    if not isinstance(contract, dict) or preflight(plan) != contract:
        raise ValueError('raw_answer_contract_changed_or_unavailable')


def messages(plan, contract):
    _current(plan, contract)
    return [
        {'role': 'system', 'content': (
            'raw-answer-v1: Extract exactly one literal value or its complete original declaration. '
            'Use only the supplied retrieved evidence. It does not prove anything outside that scope. '
            'Return the strict RawAnswerOutput JSON schema. status is answer or insufficient. '
            'For answer, unit_id and quote must identify the complete original line; coverage must '
            'spell subject, predicate, value, condition and time exactly, using empty strings for '
            'absent condition/time. Quoted values retain their paired outer quotes verbatim. '
            'Do not paraphrase, combine records, infer or omit qualifiers. '
            'If unable, use status insufficient; local verification decides admission.')},
        {'role': 'user', 'content': json.dumps({
            'raw_question_contract': {key: deepcopy(value) for key, value in contract.items()
                                      if key != 'evidence_catalog'},
            'evidence_catalog': deepcopy(contract['evidence_catalog']),
        }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))},
    ]


def validate_output(plan, contract, output):
    """Recompute every binding and render only a locally proven extract."""
    _current(plan, contract)
    value = output if isinstance(output, RawAnswerOutput) else RawAnswerOutput.model_validate(output)
    if value.status != 'answer':
        raise ValueError('raw_answer_insufficient')
    record = next((row for row in contract['records'] if row['unit_id'] == value.unit_id), None)
    if record is None or value.quote != record['quote']:
        raise ValueError('raw_answer_record_binding_invalid')
    expected = {key: record[key] for key in ('subject', 'predicate', 'value', 'condition', 'time')}
    if value.coverage.model_dump() != expected:
        raise ValueError('raw_answer_slot_coverage_invalid')
    if value.answer not in {record['value'], record['quote']}:
        raise ValueError('raw_answer_not_exact_extract')
    number = record['source_number']
    heading = '检索到的原文：' if contract['language'] == 'zh' else 'Retrieved evidence: '
    return AskOutput(answer=f'{heading}{value.answer} [{number}]', citations=[number])
