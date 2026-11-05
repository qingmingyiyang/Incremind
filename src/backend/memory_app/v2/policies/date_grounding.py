"""24有限日历语法的出处与精度校验，不替代事件语义审核。"""
from datetime import date
import re
from .date_calculation import date_provenance, calendar_values, append_date_notes, _anchor, _HEADER, _DATE
from .date_quote_boundaries import _windows

_RELATIVE_RECORD_COUNT = r'(?:[0-9]+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)'
_ARITHMETIC_DAY_COUNT = r'(?:[1-9][0-9]*|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)'


def calendar_mentions_v1(text):
    return calendar_values(text)


def _body(sentence):
    # 只去掉记录行头，不删句内主体、条件、否定或时间词。
    return re.sub(r'^\s*\[D[0-9]+:[0-9]+\]\s*[^:\r\n]{1,60}:\s*', '', sentence).strip()


def bind_date_provenance(catalog, chosen, *, include_identity=False, calendar_version=2):
    """相对式及完整原句必须落在同一真实窗口和目录单元。"""
    result = []
    for number, candidate in enumerate(chosen, 1):
        windows = _windows(candidate)
        if windows is None:
            continue
        for record in date_provenance(candidate, quote_version=3, calendar_version=calendar_version):
            if not record.get('computed') or not record.get('published') or record['note'] not in catalog['sources'][number - 1]['framing_before']:
                continue
            left, right = record['sentence_span']
            for start, end, piece, offset in windows:
                if not offset <= left < right <= offset + len(piece):
                    continue
                span = [start + left - offset, start + right - offset]
                for unit in catalog['units']:
                    if (unit['source_number'] == number and unit['coordinate_space'] == candidate.get('coordinate_space')
                            and unit['span'][0] <= span[0] < span[1] <= unit['span'][1]
                            and unit['text'][span[0] - unit['span'][0]:span[1] - unit['span'][0]] == record['sentence']):
                        bound = {'unit_id': unit['id'], 'source_number': number, 'sentence_span': span,
                            'sentence': record['sentence'], 'expression': record['expression'],
                            'anchor': record['anchor'], 'computed': record['computed'],
                            'options': record['options'], 'anchor_kind': record['anchor_kind'], 'published': True}
                        if include_identity:
                            bound.update(coordinate_space=unit['coordinate_space'], unit_span=list(unit['span']),
                                expression_span=[start + value - offset for value in record['expression_span']],
                                anchor_text=record['anchor_text'])
                        result.append(bound)
    return result


def _same(value, option):
    return all(value[key] == option[key] for key in ('start', 'end', 'precision'))


def _computed_match(value, options):
    if any(_same(value, option) for option in options):
        return True
    # 只可降低为容纳所有合法约定的完整月份，不可取周区间的某个端点。
    return (value['precision'] in {'month', 'year'} and bool(options)
            and all(value['start'] <= item['start'] <= item['end'] <= value['end'] for item in options))


def _explicit_options(quote):
    # 记录日期头只能提供参照日期，不能证明同条记录中的事件发生日。
    result = []
    for line in quote.splitlines():
        if _HEADER.match(line) or _DATE.fullmatch(line.strip()):
            continue
        result.extend(calendar_mentions_v1(line))
    return result


def validate_calendar_statements_v1(statements, catalog, chosen, question, *, record_bindings=None, _bound_anchor_context=False, _on_invalid=None, _anchor_article_context=False, _record_anchor_context=False, _frozen_record_anchors=False, _calendar_version=2, _collect_invalid=False, _arithmetic_record_anchor=False):
    """跨kind核新绝对日期；问题范围和记录锚只在明确上下文中使用。"""
    units = {unit['id']: unit for unit in catalog['units']}
    records = bind_date_provenance(catalog, chosen, calendar_version=_calendar_version)
    question_dates = calendar_mentions_v1(question)
    frozen_notes = {number: append_date_notes('', candidate, quote_version=3, calendar_version=_calendar_version).lstrip('\n')
        for number, candidate in enumerate(chosen, 1)}
    validated, invalid = [], False
    # 先审核正面日期，后审核只描述其剩余精度的limitation。
    for statement in sorted(statements, key=lambda item: item.kind == 'limitation'):
        for value in calendar_mentions_v1(statement.text):
            explicit, supported_records, cited_records = False, [], []
            notes = {note.source_number: note.text for note in statement.derived_notes}
            for evidence in statement.evidence:
                unit = units[evidence.unit_id]
                if (statement.kind == 'limitation' or evidence.role == 'support') and any(_computed_match(value, [option]) for option in _explicit_options(evidence.quote)):
                    explicit = True
                matching = []
                for record in (records if record_bindings is None else record_bindings[id(statement)]):
                    if record['unit_id'] != evidence.unit_id or _body(record['sentence']) not in evidence.quote:
                        continue
                    body = _body(record['sentence'])
                    # 同一原话在多个真实位置出现时不能猜用哪一项计算。
                    if unit['text'].count(body) != 1:
                        continue
                    matching.append(record)
                for record in matching:
                    if not _computed_match(value, record['options']):
                        continue
                    key = (record['unit_id'], tuple(record['sentence_span']), record['expression'])
                    if record_bindings is not None:
                        key += (record['coordinate_space'], tuple(record['expression_span']), record['anchor'])
                    cited_records.append(key)
                    # 本次实际计算的原句、来源与完整冻结注释必须共同绑定；别源同日不替代。
                    if (evidence.role == 'support'
                            and notes.get(record['source_number']) == frozen_notes[record['source_number']]):
                        supported_records.append(key)
                prefix = statement.text[max(0, value['span'][0] - 45):value['span'][0]]
                if (_record_anchor_context and evidence.role == 'support'
                        and _is_record_anchor_context(statement, value, evidence, unit, records)):
                    explicit = True
                if (_frozen_record_anchors
                        and _frozen_record_context_span(statement, value, evidence, unit, chosen) is not None):
                    explicit = True
                if (_bound_anchor_context and evidence.role == 'support'
                        and (_is_bound_anchor_context(statement, value, matching)
                            or _anchor_article_context and _is_article_anchor_context(statement, value, matching))):
                    explicit = True
                if (_arithmetic_record_anchor and record_bindings is not None
                        and evidence.role == 'support'
                        and notes.get(unit['source_number']) == frozen_notes[unit['source_number']]
                        and _is_record_anchor_explanation(statement, value, matching,
                                                         record_bindings[id(statement)])):
                    explicit = True
                if re.search(r'(?:relative to|as of|recorded on|session date|截至|记录于)\s*[:：]?\s*$', prefix, re.I):
                    candidate = chosen[unit['source_number'] - 1]
                    anchor_texts = [candidate.get('title', '')] + [line for line in candidate.get('excerpt', '').splitlines() if _HEADER.match(line)]
                    anchors = {anchor.isoformat() for text in anchor_texts if (anchor := _anchor(text)[0]) is not None}
                    if len(anchors) == 1 and value['precision'] == 'day' and value['start'] in anchors:
                        explicit = True
            if explicit:
                continue
            if supported_records and statement.kind == 'date':
                validated.append((value, set(supported_records)))
                continue
            if statement.kind == 'limitation':
                if any(_same(value, item) for item in question_dates):
                    continue
                if any(_same(value, item) and set(cited_records) & bound for item, bound in validated):
                    continue
            if _on_invalid is not None:
                _on_invalid(statement, value)
            if not _collect_invalid:
                raise ValueError('grounded_calendar_provenance_invalid')
            invalid = True
    if invalid:
        raise ValueError('grounded_calendar_provenance_invalid')


def _is_bound_anchor_context(statement, value, records):
    """27只认同条已引用计算的记录锚，并要求正面日期保留合法完整结果。"""
    if statement.kind != 'date' or value['precision'] != 'day':
        return False
    prefix, suffix = statement.text[:value['span'][0]], statement.text[value['span'][1]:]
    for record in records:
        if record['anchor'] != value['start']:
            continue
        week = (record['expression'].lower() in {'last week', '上周'}
                and re.search(r'\bweek\s+before\s+(?:the\s+)?$', prefix, re.I))
        record_before = (re.search(r'\bbefore\s+(?:the\s+)?$', prefix, re.I)
                and re.match(r'\s+(?:record|session|entry)(?:\s+date)?\b', suffix, re.I))
        if not (week or record_before):
            continue
        # 仅描述记录锚的句子不能让锚冒充事件日；必须同时有此计算的完整有效结果。
        if any(other['span'] != value['span'] and _computed_match(other, record['options'])
                for other in calendar_mentions_v1(statement.text)):
            return True
    return False


def _is_record_anchor_explanation(statement, value, matching, bound):
    """36 recognizes an occurrence of a bound record day as arithmetic input.

    The complete same-ref result must also be present.  This grants no event
    on the anchor day and never searches another source for a matching number.
    """
    from .date_calculation import _calculate, _number, _RELATIVE_V3, _ENGLISH_COUNT_PREFIX
    if (statement.kind != 'date' or value['precision'] != 'day'
            or len(bound) != 1 or len(matching) != 1 or matching[0] != bound[0]):
        return False
    record = bound[0]
    if (record['anchor_kind'] not in {'record_header', 'frozen_title'}
            or record['anchor'] != value['start'] or len(record['options']) != 1
            or _RELATIVE_V3.fullmatch(record['expression']) is None):
        return False
    option = record['options'][0]
    if option['precision'] != 'day' or option['start'] != option['end']:
        return False
    prefix, suffix = statement.text[:value['span'][0]], statement.text[value['span'][1]:]
    english = re.search(r'(?:(?<=\s)|(?<=[(（])|^)(' + _ARITHMETIC_DAY_COUNT + r')\s+days?\s+(before|after)\s+(?:the\s+)?$', prefix, re.I)
    chinese = re.match(r'\s*(?:的)?(前|后)([0-9一二两三四五六七八九十]+)天', suffix)
    comparative = re.match(r'\s*(早|晚)([0-9一二两三四五六七八九十]+)天', suffix) if re.search(r'比\s*$', prefix) else None
    if english:
        before_count = prefix[:english.start()]
        if _ENGLISH_COUNT_PREFIX.search(before_count):
            return False
        # Do not skip a standalone sign, operator or numeric fragment to find
        # a more convenient word farther left.  Only the nearest token counts.
        previous = before_count.split()
        if previous and not (previous[-1] in {'(', '（'} or previous[-1].isalpha()):
            return False
        count, direction = _number(english[1], calendar_version=3), english[2].lower() == 'before'
        math_start, math_end = english.start(), value['span'][1]
    elif chinese or comparative:
        relation = chinese or comparative
        count, direction = _number(relation[2], calendar_version=3), relation[1] in {'前', '早'}
        math_start = re.search(r'比\s*$', prefix).start() if comparative else value['span'][0]
        math_end = value['span'][1] + relation.end()
    else:
        return False
    if count is None or count == 0:
        return False
    try:
        anchor = date.fromisoformat(record['anchor'])
        source_result = _calculate(anchor, record['expression'], calendar_version=3)
        source_delta = (date.fromisoformat(option['start']) - anchor).days
    except (ValueError, OverflowError):
        return False
    if source_result != option['start'] or source_delta != (-count if direction else count):
        return False
    for other in calendar_mentions_v1(statement.text):
        if other['span'] == value['span'] or not _same(other, option):
            continue
        if other['span'][1] <= math_start:
            opening = re.fullmatch(r'\s*([（(])\s*', statement.text[other['span'][1]:math_start])
            closing = re.match(r'\s*([）)])', statement.text[math_end:])
            if opening and closing and (opening[1], closing[1]) in {('(', ')'), ('（', '）')}:
                return True
        if math_end <= other['span'][0]:
            bridge = statement.text[math_end:other['span'][0]]
            if re.fullmatch(r',\s*i\.e\.\s+on\s+|[，,]\s*即(?:为|是)?\s*', bridge, re.I):
                return True
    return False


def validate_calendar_statements_v2(statements, catalog, chosen, question, *, record_bindings, _on_invalid=None):
    """27在精确ref绑定下兼容有完整运算结果的自然记录锚表述。"""
    return validate_calendar_statements_v1(statements, catalog, chosen, question,
        record_bindings=record_bindings, _bound_anchor_context=True, _on_invalid=_on_invalid)


def _is_article_anchor_context(statement, value, records):
    """29只扩展精确已绑记录锚的冠词，仍须同条完整合法计算结果。"""
    if statement.kind != 'date' or value['precision'] != 'day':
        return False
    if not re.search(r'\brelative to the\s*$', statement.text[:value['span'][0]], re.I):
        return False
    return any(record['anchor'] == value['start'] and any(
        other['span'] != value['span'] and _computed_match(other, record['options'])
        for other in calendar_mentions_v1(statement.text)) for record in records)


def validate_calendar_statements_v3(statements, catalog, chosen, question, *, record_bindings, _on_invalid=None):
    return validate_calendar_statements_v1(statements, catalog, chosen, question,
        record_bindings=record_bindings, _bound_anchor_context=True,
        _anchor_article_context=True, _on_invalid=_on_invalid)


def _record_anchor_label_span(text, value):
    """日期只修饰记录或会话；事件的on日期及无标签before日期不在此语法中。"""
    prefix, suffix = text[:value['span'][0]], text[value['span'][1]:]
    for before, after in (
        (r'\b(?:relative to|as of)\s+(?:the\s+)?$', r'\s+(?:record|conversation|session)(?:\s+date)?\b'),
        (r'(?:相对于|以)\s*$', r'\s*的?(?:记录|会话)日期'),
    ):
        left, right = re.search(before, prefix, re.I), re.match(after, suffix, re.I)
        if left and right:
            return left.start(), value['span'][1] + right.end()
    left = re.search(r'\b(?:record|conversation|session)\s+date\s*(?:is|was|:)\s*$|(?:记录|会话)日期\s*(?:为|是|:|：)\s*$', prefix, re.I)
    return (left.start(), value['span'][1]) if left else None


def _labeled_record_anchor(text, value):
    return _record_anchor_label_span(text, value) is not None


def _is_record_anchor_context(statement, value, evidence, unit, records):
    """30从同一真实原句取记录锚，只解释上下文，绝不写入计算bindings。"""
    if statement.kind not in {'fact', 'inference'} or value['precision'] != 'day':
        return False
    matching = [record for record in records if record['unit_id'] == evidence.unit_id
        and record['anchor'] == value['start'] and _body(record['sentence']) in evidence.quote
        and evidence.quote in unit['text'] and unit['text'].count(_body(record['sentence'])) == 1]
    if not matching:
        return False
    if _labeled_record_anchor(statement.text, value):
        return True
    # 同条先明确记录日期后，可重复引用该锚说明原话的上/下周；不是事件绝对日期。
    for other in calendar_mentions_v1(statement.text):
        if (other['span'][1] <= value['span'][0] and _same(value, other)
                and _labeled_record_anchor(statement.text, other)):
            prefix = statement.text[:value['span'][0]]
            return any((record['expression'].lower() in {'last week', '上周'}
                and re.search(r'\bweek\s+before\s+(?:the\s+)?$', prefix, re.I))
                or (record['expression'].lower() in {'next week', '下周'}
                and re.search(r'\bweek\s+after\s+(?:the\s+)?$', prefix, re.I)) for record in matching)
    return False


def _frozen_record_anchor(candidate):
    """33只取同一冻结来源唯一的真实标题或非引语日期头，不依赖能否计算相对式。"""
    from .date_quote_boundaries import quote_spans
    texts = []
    title = candidate.get('title', '')
    if _DATE.search(title):
        texts.append(title)
    quotes, offset = quote_spans(candidate, quote_version=3), 0
    for raw in candidate.get('excerpt', '').splitlines(keepends=True):
        line, left = raw.rstrip('\r\n'), offset
        offset += len(raw)
        if (not any(start < left + len(line) and end > left for start, end in quotes)
                and (_HEADER.match(line) or _DATE.fullmatch(line.strip()))):
            texts.append(line)
    anchors = [_anchor(text)[0] for text in texts]
    return anchors[0].isoformat() if anchors and None not in anchors and len(set(anchors)) == 1 else None


def _relative_record_offset_span(prefix, quote):
    """只复述同条引文的相对距离；不生成绝对日期或更细精度。"""
    match = re.search(r'\b(' + _RELATIVE_RECORD_COUNT + r')\s+(days?|weeks?|months?|years?)\s+(before|after)\s+(?:the\s+)?$', prefix, re.I)
    if match is None:
        return None
    direction = 'ago' if match[3].lower() == 'before' else 'later'
    source = r'\b' + re.escape(match[1]) + r'\s+' + re.escape(match[2]) + r'\s+' + direction + r'\b'
    return match.start() if re.search(source, quote, re.I) else None


def _relative_record_duration_span(prefix, quote):
    """同条原话的持续时长只可复述到已核实的会话日期，不推算开始日。"""
    match = re.search(r'\bfor\s+(' + _RELATIVE_RECORD_COUNT + r')\s+(days?|weeks?|months?|years?)\s+as\s+of\s+(?:the\s+)?$', prefix, re.I)
    if match is None:
        return None
    source = r'\bfor\s+' + re.escape(match[1]) + r'\s+' + re.escape(match[2]) + r'\s+now\b'
    return match.start() if re.search(source, quote, re.I) else None


def _frozen_record_context_span(statement, value, evidence, unit, chosen):
    """同单元真实引文只授权明确标注的会话元数据，limitation可用context引用。"""
    if (statement.kind not in {'fact', 'inference', 'limitation'} or value['precision'] != 'day'
            or evidence.role != 'support' and statement.kind != 'limitation'
            or not evidence.quote.strip() or evidence.quote not in unit['text']
            or _frozen_record_anchor(chosen[unit['source_number'] - 1]) != value['start']):
        return None
    label = _record_anchor_label_span(statement.text, value)
    if label is not None:
        return label
    prefix, suffix = statement.text[:value['span'][0]], statement.text[value['span'][1]:]
    right = re.match(r'\s+(?:record|conversation|session)(?:\s+date)?\b', suffix, re.I)
    if right is not None:
        offset = _relative_record_offset_span(prefix, evidence.quote)
        if offset is not None:
            return offset, value['span'][1] + right.end()
        left = re.search(r'\b(?:in|during|from)\s+(?:the\s+)?$', prefix, re.I)
        if left is not None:
            return left.start(), value['span'][1] + right.end()
    left = re.search(r'\b(?:record|conversation|session)\s+(?:on|dated)\s*$', prefix, re.I)
    return (left.start(), value['span'][1]) if left is not None else None


def _record_context_temporal_residue(statement, evidence, unit, records, *, chosen=None):
    """只移除已核实的完整锚语法和原话相对式；剩余时间指代不借记录锚获授权。"""
    values = calendar_mentions_v1(statement.text)
    contexts = [value for value in values if (_is_record_anchor_context(statement, value, evidence, unit, records)
        or chosen is not None and _frozen_record_context_span(statement, value, evidence, unit, chosen) is not None)]
    if not contexts:
        return False
    spans = [value['span'] for value in values]
    for value in contexts:
        label = (_frozen_record_context_span(statement, value, evidence, unit, chosen) if chosen is not None
            else _record_anchor_label_span(statement.text, value))
        if label is not None:
            spans.append(label)
            if chosen is not None:
                duration = _relative_record_duration_span(statement.text[:value['span'][0]], evidence.quote)
                if duration is not None:
                    spans.append((duration, value['span'][0]))
        else:
            # 已由同条真实原句核过的重复上/下周参照，只移除本次完整短语。
            week = re.search(r'\bweek\s+(?:before|after)\s+(?:the\s+)?$', statement.text[:value['span'][0]], re.I)
            if week:
                spans.append((week.start(), value['span'][1]))
    for record in records:
        if (record['unit_id'] != evidence.unit_id or _body(record['sentence']) not in evidence.quote
                or record['anchor'] not in {value['start'] for value in contexts}):
            continue
        expressions = {record['expression']}
        if record['expression'].lower() in {'last week', '上周'}:
            expressions.update({'last week', '上周'})
        elif record['expression'].lower() in {'next week', '下周'}:
            expressions.update({'next week', '下周'})
        for expression in expressions:
            pattern = re.escape(expression)
            if expression.isascii():
                pattern = r'\b' + pattern + r'\b'
            spans.extend(match.span() for match in re.finditer(pattern, statement.text, re.I))
    residual = list(statement.text)
    for left, right in spans:
        residual[left:right] = ' ' * (right - left)
    # 这是声明级有限时间语法，不做任意语言推理；未分类的额外时间或会话指代保守拒绝。
    return re.search(r'\b(?:days?|dates?|times?|weeks?|months?|years?|hours?|minutes?|seconds?|nights?|'
        r'mornings?|afternoons?|evenings?|moments?|instants?|today|tonight|yesterday|tomorrow|then|'
        r'record(?:ed)?|conversations?|sessions?)\b|\b(?:this|that|same)\s+point\b|'
        r'日期|时间|时候|时刻|会话|记录|(?:这|那|该|此|同|当|其|彼)(?:一|个|一个)?(?:天|日|时)|'
        r'今天|昨天|明天|今夜|昨夜|彼时|届时', ''.join(residual), re.I) is not None


def validate_calendar_statements_v4(statements, catalog, chosen, question, *, record_bindings, _on_invalid=None, _frozen_record_anchors=False, _calendar_version=2, _collect_invalid=False, _arithmetic_record_anchor=False):
    """明确记录锚不算事件日；模糊当天仍需原文独立证明，不能借锚授权。"""
    units = {unit['id']: unit for unit in catalog['units']}
    records = bind_date_provenance(catalog, chosen, calendar_version=_calendar_version)
    invalid = False
    for statement in statements:
        if statement.kind not in {'fact', 'inference'}:
            continue
        values = calendar_mentions_v1(statement.text)
        # 原文独立明示的事件日继续走显式证据路径，不依赖新记录锚例外。
        unsupported = [value for value in values if not any(evidence.role == 'support'
            and any(_computed_match(value, [option]) for option in _explicit_options(evidence.quote))
            for evidence in statement.evidence)]
        if unsupported and any(evidence.role == 'support' and _record_context_temporal_residue(
                statement, evidence, units[evidence.unit_id], records,
                chosen=chosen if _frozen_record_anchors else None) for evidence in statement.evidence):
            if _on_invalid is not None:
                for value in (unsupported if _collect_invalid else unsupported[:1]):
                    _on_invalid(statement, value)
            if not _collect_invalid:
                raise ValueError('grounded_calendar_provenance_invalid')
            invalid = True
    for statement in statements:
        if statement.kind not in {'fact', 'inference', 'date'} or not re.search(
                r'\b(?:this|that|same|the\s+same|recorded)\s+(?:day|date|time|moment|instant|occasion)\b|'
                r'\b(?:day|date|time)\s+of\s+(?:this|that|the)\s+(?:record|conversation|session)\b|'
                r'\bon\s+the\s+date\b|(?:这|那|该|此|同|当|其|彼)(?:一|个|一个)?(?:天|日|时)|同日', statement.text, re.I):
            continue
        for value in calendar_mentions_v1(statement.text):
            if not any(evidence.role == 'support' and any(_computed_match(value, [option])
                    for option in _explicit_options(evidence.quote)) for evidence in statement.evidence):
                if _on_invalid is not None:
                    _on_invalid(statement, value)
                if not _collect_invalid:
                    raise ValueError('grounded_calendar_provenance_invalid')
                invalid = True
    validate_calendar_statements_v1(statements, catalog, chosen, question,
        record_bindings=record_bindings, _bound_anchor_context=True, _anchor_article_context=True,
        _record_anchor_context=True, _on_invalid=_on_invalid, _frozen_record_anchors=_frozen_record_anchors,
        _calendar_version=_calendar_version, _collect_invalid=_collect_invalid,
        _arithmetic_record_anchor=_arithmetic_record_anchor)
    if invalid:
        raise ValueError('grounded_calendar_provenance_invalid')


def validate_calendar_statements_v5(statements, catalog, chosen, question, *, record_bindings, _on_invalid=None):
    """33允许同源会话日期上下文，计算、事件日期与剩余时间指代仍走原核验。"""
    return validate_calendar_statements_v4(statements, catalog, chosen, question,
        record_bindings=record_bindings, _on_invalid=_on_invalid, _frozen_record_anchors=True)


def validate_calendar_statements_v6(statements, catalog, chosen, question, *, record_bindings, _on_invalid=None, _collect_invalid=False):
    """34只增加英文日偏移；诊断可收集全部非法值，末稿仍遇错即拒。"""
    return validate_calendar_statements_v4(statements, catalog, chosen, question,
        record_bindings=record_bindings, _on_invalid=_on_invalid, _frozen_record_anchors=True,
        _calendar_version=3, _collect_invalid=_collect_invalid)


def validate_calendar_statements_v7(statements, catalog, chosen, question, *, record_bindings, _on_invalid=None, _collect_invalid=False):
    """36 only adds a fully bound same-offset arithmetic record-anchor input."""
    return validate_calendar_statements_v4(statements, catalog, chosen, question,
        record_bindings=record_bindings, _on_invalid=_on_invalid, _frozen_record_anchors=True,
        _calendar_version=3, _collect_invalid=_collect_invalid, _arithmetic_record_anchor=True)


def build_date_ref_catalog(catalog, chosen, *, calendar_version=2):
    """26只为当轮冻结窗口中的真实已发表计算分配稳定位置编号。"""
    records = sorted(bind_date_provenance(catalog, chosen, include_identity=True, calendar_version=calendar_version),
        key=lambda item: (item['source_number'], tuple(item['sentence_span']), item['expression'], item['anchor'], item['unit_id']))
    return [dict(record, date_ref_id=index) for index, record in enumerate(records, 1)]


def resolve_date_refs(statement, catalog, chosen, refs, *, compose_version='@26', _on_invalid=None):
    """只解析模型明示编号，不猜别名、来源、原句或未提供的计算引用。"""
    units = {unit['id']: unit for unit in catalog['units']}
    records = {record['date_ref_id']: record for record in refs}
    resolved, seen = [], set()
    def reject(position, reason, record=None):
        if _on_invalid is not None:
            issue = {'field_path': f'date_refs[{position}]', 'condition': reason,
                'repair': 'Remove this ref and unsupported computed text, or cite its complete real sentence in the matching unit; otherwise exclude.'}
            if record is not None:
                issue['required_evidence'] = {key: record[key] for key in ('unit_id', 'source_number', 'coordinate_space', 'sentence_span')}
                issue['required_evidence']['quote'] = _body(record['sentence'])
            _on_invalid(issue)
        raise ValueError('grounded_date_ref_invalid')
    for position, ref_id in enumerate(statement.date_refs):
        if type(ref_id) is not int or ref_id in seen or ref_id not in records:
            reject(position, 'unknown_or_duplicate_ref')
        seen.add(ref_id)
        record = records[ref_id]
        unit = units.get(record['unit_id'])
        if (unit is None or unit['source_number'] != record['source_number']
                or unit['coordinate_space'] != record['coordinate_space'] or unit['span'] != record['unit_span']
                or not record['sentence_span'][0] <= record['expression_span'][0] < record['expression_span'][1] <= record['sentence_span'][1]
                or unit['text'][record['expression_span'][0] - unit['span'][0]:record['expression_span'][1] - unit['span'][0]] != record['expression']
                or unit['text'][record['sentence_span'][0] - unit['span'][0]:record['sentence_span'][1] - unit['span'][0]] != record['sentence']
                or not any(item.unit_id == record['unit_id'] and _body(record['sentence']) in item.quote
                    and item.quote in unit['text'] for item in statement.evidence)
                or unit['text'].count(_body(record['sentence'])) != 1):
            reject(position, 'ref_requires_matching_unit_and_complete_sentence', record)
        resolved.append(record)
    # 28先完整验ref，再把普通声明视为没有计算引用；不改原件，也不授权后续精度说明。
    if compose_version in {'@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'} and statement.kind in {'fact', 'inference'}:
        return [], []
    # 完整旧注释由同一冻结来源重建，模型无需也不能改写它。
    numbers = list(dict.fromkeys(record['source_number'] for record in resolved))
    notes = [] if (statement.kind == 'limitation' or compose_version == '@27' and statement.kind != 'date') else [
        {'source_number': number, 'text': append_date_notes('', chosen[number - 1], quote_version=3, calendar_version=3 if compose_version in {'@34', '@35', '@36'} else 2).lstrip('\n')}
        for number in numbers]
    return resolved, notes
