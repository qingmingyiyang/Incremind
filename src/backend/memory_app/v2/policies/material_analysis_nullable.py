"""材料第三版允许明确无时间原词的事件；旧时间解析与提示词保持冻结。"""
from dataclasses import dataclass
import json
import re

from .date_calculation import _DATE, _HEADER, _RELATIVE, calendar_values
from .material_analysis import _object, _quote, _quoted, _strings, _text, decode, parse_temporal
from .material_analysis_fallback import MaterialAnalysisFallbackPolicy, parse_focused, read_requests


def _direct_time_omitted(event, source, source_offset):
    """只识别有明确局部时间用法的遗漏，不由未命中推断事件没有时间。"""
    quoted = _quoted(source)
    for relative, pattern in ((True, _RELATIVE), (False, _DATE)):
        for match in pattern.finditer(event['quote']):
            position = event['start'] - source_offset + match.start()
            if any(left <= position < right for left, right in quoted):
                continue
            line_start = source.rfind('\n', 0, position) + 1
            line_end = source.find('\n', position)
            line = source[line_start:line_end if line_end >= 0 else len(source)]
            if _HEADER.match(line):
                continue
            suffix = source[position + len(match.group()):]
            # 资料所属日期和事件时间不同，不能因日期修饰资料名而强迫赋予事件日期。
            if re.match(r"\s*(?:['’]s\s+)?(?:notes?|records?|reports?|articles?|documents?)\b|"
                    r'\s*(?:的)?(?:笔记|记录|报告|文章|资料|文档)', suffix, re.I):
                continue
            if relative:
                return True
            prefix = source[line_start:position]
            sentence_prefix = re.split(r'[。！？]|[.!?](?=\s|$)', prefix)[-1]
            # 仅直接时间用法进入日期校验；参考编号的形状不授予事件日期资格。
            if sentence_prefix.strip() and not re.search(r'(?:\bon\s+|于|在|截至)\s*$', prefix, re.I):
                continue
            if calendar_values(match.group()):
                return True
    return False


def parse_nullable_temporal(raw, *, piece, offset, original_title, full_source=None, source_offset=0):
    value = _object(decode(raw), ('events',))
    if not isinstance(value['events'], list):
        raise ValueError('material_analysis_invalid')
    source = piece if full_source is None else full_source
    source_offset = offset if full_source is None else source_offset
    if source[offset - source_offset:offset - source_offset + len(piece)] != piece:
        raise ValueError('material_analysis_binding_changed')
    result = []
    for row in value['events']:
        _object(row, ('actors', 'event_text', 'quote', 'expression', 'anchor_quote', 'anchor_role', 'event_status'))
        if row['expression'] is not None:
            # 有时间原词完全委托旧合同，保持记录头作用域、引语与确定计算器语义。
            result.extend(parse_temporal(json.dumps({'events': [row]}, ensure_ascii=False), piece=piece,
                offset=offset, original_title=original_title, full_source=source, source_offset=source_offset))
            continue
        if (row['anchor_quote'] is not None or row['anchor_role'] != 'unknown'
                or not isinstance(row['event_status'], str)
                or row['event_status'] not in {'asserted', 'planned', 'missed', 'conditional', 'question', 'unknown'}):
            raise ValueError('material_analysis_invalid')
        evidence = _quote(piece, offset, row['quote'])
        if _direct_time_omitted(evidence, source, source_offset):
            raise ValueError('material_analysis_time_expression_omitted')
        result.append({'actors': _strings(row['actors']), 'event_text': _text(row['event_text']),
            'evidence': evidence, 'expression': None,
            'anchor_role': 'unknown', 'anchor_evidence': None, 'record_anchor': None,
            'event_status': row['event_status'], 'computed': None,
            'event_time': {'status': 'unknown', 'precision': 'unknown', 'options': []},
            'calculation_version': None})
    return result


def _nullable_prompt(instructions):
    return instructions.replace('expression须在quote中；',
        '有时间原词时expression须在quote中连续且唯一；没有时间原词时仍保留事件，'
        'expression必须为null，同时anchor_quote=null、anchor_role=unknown，事件时间保持未知，'
        '明确时间原词不得省略为null；资料所属日期和历史引语日期不得混作当前事件时间；'
        '不能借记录日期或相邻事件补造时间，不能将事件自身的陈述/计划等状态改为unknown；') + (
        '无时间原词事件的独立变体示例：{"actors":["主体原词"],"event_text":"完整事件陈述",'
        '"quote":"无时间原词的事件原话","expression":null,"anchor_quote":null,"anchor_role":"unknown",'
        '"event_status":"asserted|planned|missed|conditional|question|unknown"}。')


@dataclass(frozen=True)
class MaterialAnalysisNullablePolicy(MaterialAnalysisFallbackPolicy):
    def __call__(self, operation, **values):
        messages = super().__call__(operation, **values)
        if operation == 'temporal':
            return [{**messages[0], 'content': _nullable_prompt(messages[0]['content'])}, messages[1]]
        return messages

    def instructions(self, operation):
        instructions = super().instructions(operation)
        return _nullable_prompt(instructions) if operation == 'temporal' else instructions

    def parse_temporal(self, raw, **options):
        return parse_nullable_temporal(raw, **options)

    def parse_focused(self, raw, *, operation, piece, offset, plan, original_title, full_source, source_offset):
        if operation != 'temporal':
            return parse_focused(raw, operation=operation, piece=piece, offset=offset, plan=plan,
                original_title=original_title, full_source=full_source, source_offset=source_offset)
        value = _object(decode(raw), ('events', 'read_requests'))
        requests = read_requests(value['read_requests'])
        parsed = parse_nullable_temporal(json.dumps({'events': value['events']}, ensure_ascii=False),
            piece=piece, offset=offset, original_title=original_title,
            full_source=full_source, source_offset=source_offset)
        return {'value': parsed, 'read_requests': requests}


v3 = MaterialAnalysisNullablePolicy()
