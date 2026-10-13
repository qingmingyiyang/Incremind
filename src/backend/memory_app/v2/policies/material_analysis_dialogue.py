"""第四版只分离明确相邻对话单元的完整正文，不推测省略内容或主体语义。"""
from dataclasses import dataclass
import json
import re

from .material_analysis import _object, _text, decode, parse_claims
from .material_analysis_fallback import read_requests
from .material_analysis_nullable import MaterialAnalysisNullablePolicy


_DIALOGUE = re.compile(r'^\[(?P<id>[^\]\r\n]+)\][ \t]+(?P<speaker>[^:\r\n]+):[ \t]*(?P<body>[^\r\n]+)$')
_ANALYSIS = ('每条evidence.quote须独立连续且唯一，非连续依据须给多个evidence，'
    '不能删去中间发言头后把多人原话拼成一条quote。保留各说话人归属，不将共同引用当成同一人陈述。')


def _units(piece, offset, source, source_offset):
    units, start = [], 0
    for number, line in enumerate(piece.splitlines(keepends=True)):
        text = line.rstrip('\r\n')
        match = _DIALOGUE.fullmatch(text)
        relative = offset - source_offset + start
        # 重点段外的发言头或被截断的正文不能补成一个完整单元。
        complete_start = relative == 0 or source[relative - 1] == '\n'
        end = relative + len(text)
        complete_end = end == len(source) or source[end:end + 1] in {'\r', '\n'}
        if (match and match['id'].strip() and match['speaker'].strip()
                and complete_start and complete_end):
            body = match['body'].strip()
            if body:
                body_start = start + match.start('body') + len(match['body']) - len(match['body'].lstrip())
                units.append({'line': number, 'body': body, 'start': body_start,
                    'end': body_start + len(body), 'header_start': start})
        start += len(line)
    return units


def _split_quote(quote, *, piece, offset, source, source_offset):
    units = _units(piece, offset, source, source_offset)
    matches = []
    for index, first in enumerate(units):
        if not quote.startswith(first['body'] + ' '):
            continue
        joined, selected = first['body'], [first]
        for unit in units[index + 1:]:
            previous = selected[-1]
            gap = piece[previous['end']:unit['header_start']]
            if (unit['line'] != previous['line'] + 1
                    or re.fullmatch(r'[ \t]*\r?\n[ \t]*', gap) is None):
                break
            joined += ' ' + unit['body']
            selected.append(unit)
            if joined == quote:
                matches.append(tuple(selected))
            if len(joined) >= len(quote):
                break
    if len(matches) != 1 or any(source.count(unit['body']) != 1 for unit in matches[0]):
        raise ValueError('material_analysis_quote_invalid')
    return [{'quote': unit['body']} for unit in matches[0]]


def parse_dialogue_claims(raw, *, piece, offset, plan, full_source=None, source_offset=0):
    try:
        return parse_claims(raw, piece=piece, offset=offset, plan=plan)
    except ValueError as error:
        if str(error) != 'material_analysis_quote_invalid':
            raise
    source = piece if full_source is None else full_source
    source_offset = offset if full_source is None else source_offset
    if source[offset - source_offset:offset - source_offset + len(piece)] != piece:
        raise ValueError('material_analysis_binding_changed')
    value = _object(decode(raw), ('claims',))
    for row in value['claims']:
        _object(row, ('angle_ids', 'text', 'epistemic_status', 'event_status', 'actors', 'entities', 'evidence'))
        if not isinstance(row['evidence'], list):
            raise ValueError('material_analysis_invalid')
        evidence = []
        for item in row['evidence']:
            _object(item, ('quote',))
            quote = _text(item['quote'])
            if piece.count(quote):
                evidence.append(item)
            else:
                evidence.extend(_split_quote(quote, piece=piece, offset=offset,
                    source=source, source_offset=source_offset))
        row['evidence'] = evidence
    # 原严格解析最终核全部字段、每条唯一原话与实际坐标，原模型输出仍留在Turn检查点。
    return parse_claims(json.dumps(value, ensure_ascii=False), piece=piece, offset=offset, plan=plan)


@dataclass(frozen=True)
class MaterialAnalysisDialoguePolicy(MaterialAnalysisNullablePolicy):
    def __call__(self, operation, **values):
        messages = super().__call__(operation, **values)
        if operation == 'analysis':
            return [{**messages[0], 'content': messages[0]['content'] + _ANALYSIS}, messages[1]]
        return messages

    def instructions(self, operation):
        instructions = super().instructions(operation)
        return instructions + _ANALYSIS if operation == 'analysis' else instructions

    def parse_claims(self, raw, **options):
        return parse_dialogue_claims(raw, **options)

    def parse_focused(self, raw, *, operation, piece, offset, plan, original_title, full_source, source_offset):
        if operation != 'analysis':
            return super().parse_focused(raw, operation=operation, piece=piece, offset=offset,
                plan=plan, original_title=original_title, full_source=full_source, source_offset=source_offset)
        value = _object(decode(raw), ('claims', 'read_requests'))
        requests = read_requests(value['read_requests'])
        parsed = parse_dialogue_claims(json.dumps({'claims': value['claims']}, ensure_ascii=False),
            piece=piece, offset=offset, plan=plan, full_source=full_source, source_offset=source_offset)
        return {'value': parsed, 'read_requests': requests}


v4 = MaterialAnalysisDialoguePolicy()
