"""单次回答的私有引文合同；公开结果继续使用 AskOutput。"""
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from ..structured_generation import AskOutput

DERIVED_NOTES = '确定日期计算（程序派生注释，不是原文；只解释以上资料，不提供额外事实）：'


class EvidenceQuote(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_number: Annotated[StrictInt, Field(ge=1, description='Original source heading [N], not quote position.')]
    quote: Annotated[StrictStr, Field(min_length=1, description='Short continuous raw source quote.')]


class EvidenceFirstAskOutput(BaseModel):
    # 独立模型保留 evidence 在前的字段顺序，不改变公开 AskOutput。
    model_config = ConfigDict(extra='forbid', strict=True, json_schema_extra={
        'examples': [{
            'evidence': [{'source_number': 2, 'quote': 'I repaired the bicycle.'}],
            'answer': 'The bicycle was repaired.', 'citations': [2],
        }],
    })
    evidence: list[EvidenceQuote]
    answer: StrictStr
    citations: list[Annotated[StrictInt, Field(ge=1)]]

    @field_validator('answer')
    @classmethod
    def public_answer(cls, value):
        return AskOutput(answer=value, citations=[]).answer



class ExtractedSourceQuotes(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_number: Annotated[StrictInt, Field(ge=1)]
    quotes: list[Annotated[StrictStr, Field(min_length=1)]]


class SourceQuoteExtraction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    sources: list[ExtractedSourceQuotes]


class EvidencePartitionOutput(BaseModel):
    # 编号由程序冻结，模型只分类，不能自行生成原文或坐标。
    model_config = ConfigDict(extra='forbid', strict=True)
    selected_ids: list[StrictInt]
    irrelevant_ids: list[StrictInt]
    uncertain_ids: list[StrictInt]


def verified_quote_bundle(output, selected_sources):
    """仅验证出处并保留已选连续原文的整行语气，不声称语义完整。"""
    value = SourceQuoteExtraction.model_validate(output)
    numbers = [row.source_number for row in value.sources]
    if sorted(numbers) != list(range(1, len(selected_sources) + 1)):
        raise ValueError('source_quote_coverage_invalid')
    result = []
    for row in sorted(value.sources, key=lambda item: item.source_number):
        source = selected_sources[row.source_number - 1]
        # 原件仅接受已证明对应 entry.content 的坐标空间，未知空间不能绕过正文核验。
        if source.get('kind') == 'source' and source.get('coordinate_space') != 'source_content_v1':
            raise ValueError('source_quote_coordinate_space_unsupported')
        excerpt = source.get('excerpt')
        if not isinstance(excerpt, str) or DERIVED_NOTES in excerpt:
            raise ValueError('source_quote_raw_unavailable')
        windows = source.get('windows')
        if not isinstance(windows, (list, tuple)) or not windows:
            raise ValueError('source_quote_windows_unavailable')
        previous = None
        for window in windows:
            start, end, text = (getattr(window, key, None) for key in ('start', 'end', 'text'))
            if (type(start) is not int or type(end) is not int or not 0 <= start < end
                    or not isinstance(text, str) or len(text) != end - start
                    or (previous is not None and start < previous)):
                raise ValueError('source_quote_windows_invalid')
            if source.get('kind') == 'source' and source.get('coordinate_space') == 'source_content_v1':
                content = source.get('entry', {}).get('content')
                if not isinstance(content, str) or end > len(content) or text != content[start:end]:
                    raise ValueError('source_quote_windows_invalid')
            previous = end
        if source.get('kind') == 'source' and excerpt != '\n…\n'.join(window.text for window in windows):
            raise ValueError('source_quote_windows_invalid')
        # 仅已证 source 坐标空间可合并零缺口窗口，最大链避免同位置短截版掩盖问尾。
        mergeable = source.get('kind') == 'source' and source.get('coordinate_space') == 'source_content_v1'
        chains = []
        for window in windows:
            if mergeable and chains and chains[-1]['end'] == window.start:
                chains[-1]['end'] = window.end
                chains[-1]['parts'].append(window.text)
            else:
                chains.append({'start': window.start, 'end': window.end, 'parts': [window.text]})
        contexts = []
        for quote in row.quotes:
            if not quote.strip() or DERIVED_NOTES in quote:
                raise ValueError('source_quote_invalid')
            found = False
            for chain in chains:
                parts = chain['parts']
                text = ''.join(parts)
                raw_quote = quote
                if quote not in text:
                    if not mergeable or not _matches_continuous_quote(quote, parts):
                        continue
                    raw_quote = quote.replace('\n…\n', '')
                start = text.find(raw_quote)
                while start >= 0:
                    found = True
                    end = start + len(raw_quote)
                    left = text.rfind('\n', 0, start) + 1
                    right = text.find('\n', end)
                    right = len(text) if right < 0 else right
                    content = source.get('entry', {}).get('content') if mergeable else None
                    item = {'quote': quote, 'context': text[left:right],
                            'quote_span': [chain['start'] + start, chain['start'] + end],
                            'context_span': [chain['start'] + left, chain['start'] + right],
                            'chain_span': [chain['start'], chain['end']],
                            'before': 'omitted' if chain['start'] > 0 else 'record_start',
                            'after': ('record_end' if isinstance(content, str) and chain['end'] == len(content)
                                      else 'omitted' if isinstance(content, str) else 'selected_end')}
                    if item not in contexts:
                        contexts.append(item)
                    start = text.find(raw_quote, start + 1)
            if not found:
                raise ValueError('source_quote_invalid')
        result.append({'source_number': row.source_number, 'quotes': contexts})
    return {'sources': result}

def answer_response_model(compose_version, *, frozen_schema=None):
    """旧策略保持原合同；续答只能恢复实际冻结过的模型。"""
    if frozen_schema is not None:
        for model in (AskOutput, EvidenceFirstAskOutput, SourceQuoteExtraction, EvidencePartitionOutput, GroundedAnswerOutput, GroundedUsageAnswerOutput, GroundedScopeAnswerOutput, FirstFactsOutput, ReviewedScopeAnswerOutput, FirstReferencedFactsOutput, ReviewedReferencedScopeOutput, FocusedReferencedFactsOutput, FocusedReviewedScopeOutput, ReconstructedReviewedOutput):
            if frozen_schema == model.model_json_schema():
                return model
        raise ValueError('answer_response_schema_unavailable')
    return (FocusedReferencedFactsOutput if compose_version in {'@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'} else FirstReferencedFactsOutput if compose_version == '@26' else FirstFactsOutput if compose_version in {'@24', '@25', '@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'} else EvidencePartitionOutput if compose_version in {'@18', '@19', '@20', '@21', '@22', '@23'} else
            SourceQuoteExtraction if compose_version in {'@15', '@16', '@17'} else
            EvidenceFirstAskOutput if compose_version in ('@10', '@11') else AskOutput)


def _continuous_display_chains(source):
    """只证明原文零缺口窗口链的显示等价，不把显示省略号声称为原文字节。"""
    if source.get('kind') != 'source' or source.get('coordinate_space') != 'source_content_v1':
        return ()
    entry = source.get('entry')
    content = entry.get('content') if isinstance(entry, Mapping) else None
    windows = source.get('windows')
    if not isinstance(content, str) or not isinstance(windows, (list, tuple)) or not windows:
        return ()
    separator = '\n…\n'
    previous_end, groups, texts = None, [], []
    for window in windows:
        start, end, text = (getattr(window, name, None) for name in ('start', 'end', 'text'))
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(content)
                or not isinstance(text, str) or text != content[start:end] or separator in text
                or (previous_end is not None and start < previous_end)):
            return ()
        if previous_end is None or start != previous_end:
            groups.append([])
        groups[-1].append(text)
        texts.append(text)
        previous_end = end
    if source.get('excerpt') != separator.join(texts):
        return ()
    return tuple(tuple(group) for group in groups if len(group) > 1)


def _matches_continuous_quote(quote, group):
    if quote in ''.join(group):
        return True
    separator = '\n…\n'
    if separator not in quote:
        return False
    display, spans, offset = separator.join(group), [], 0
    for text in group:
        spans.append((offset, offset + len(text)))
        offset += len(text) + len(separator)
    start = display.find(quote)
    while start >= 0:
        end, crossed, valid = start + len(quote), False, True
        for left, right in zip(spans, spans[1:]):
            marker_start, marker_end = left[1], right[0]
            if start < marker_end and end > marker_start:
                crossed = True
                # 显示引文必须完整跨过标记，且两侧各含真实非空白原文。
                if (start >= marker_start or end <= marker_end
                        or not display[max(start, left[0]):marker_start].strip()
                        or not display[marker_end:min(end, right[1])].strip()):
                    valid = False
                    break
        if crossed and valid:
            return True
        start = display.find(quote, start + 1)
    return False


def accept_evidence_structure(output, selected_sources: Sequence[Mapping]) -> AskOutput:
    """校验连续原引文与编号关联；不把出处校验声称为语义核验。"""
    value = (output if isinstance(output, EvidenceFirstAskOutput)
             else EvidenceFirstAskOutput.model_validate(output))
    raw_windows = {}
    for source_number, source in enumerate(selected_sources, 1):
        excerpt = source.get('excerpt')
        if not isinstance(excerpt, str) or not excerpt.strip() or DERIVED_NOTES in excerpt:
            raise ValueError('evidence_first_raw_excerpt_unavailable')
        raw_windows[source_number] = tuple(excerpt.split('\n…\n'))
    for evidence in value.evidence:
        windows = raw_windows.get(evidence.source_number, ())
        matched = any(evidence.quote in window for window in windows)
        if not matched and evidence.source_number in raw_windows:
            matched = any(_matches_continuous_quote(evidence.quote, chain) for chain in
                          _continuous_display_chains(selected_sources[evidence.source_number - 1]))
        if (not evidence.quote.strip() or evidence.source_number not in raw_windows
                or DERIVED_NOTES in evidence.quote
                or not matched):
            raise ValueError('evidence_first_raw_quote_invalid')
    evidence_numbers = {evidence.source_number for evidence in value.evidence}
    if any(source_number not in evidence_numbers for source_number in value.citations):
        raise ValueError('evidence_first_citation_without_quote')
    return AskOutput(answer=value.answer, citations=list(value.citations))


class CoverageOutput(BaseModel):
    # 末轮仅决定补充原话；所有首轮已验收单位必须原样保留。
    model_config = ConfigDict(extra='forbid', strict=True)
    mandatory_ids: list[StrictInt]
    additional_quotes: list[ExtractedSourceQuotes]


def coverage_required(compose_version, question):
    from .policies.ladder import multi_evidence
    return compose_version == '@17' and isinstance(question, str) and multi_evidence(question)


def final_response_model(compose_version, question):
    if compose_version == '@32':
        return ReconstructedReviewedOutput
    if compose_version in {'@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        return FocusedReviewedScopeOutput
    if compose_version == '@26':
        return ReviewedReferencedScopeOutput
    if compose_version in {'@24', '@25', '@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        return ReviewedScopeAnswerOutput
    if compose_version == '@23':
        return GroundedScopeAnswerOutput
    if compose_version == '@22':
        return GroundedUsageAnswerOutput
    if compose_version in {'@20', '@21'}:
        return GroundedAnswerOutput
    if compose_version in {'@18', '@19'}:
        from .policies.ladder import multi_evidence
        return EvidencePartitionOutput if isinstance(question, str) and multi_evidence(question) else AskOutput
    return CoverageOutput if coverage_required(compose_version, question) else AskOutput


def coverage_units(extracted, chosen):
    """按已选整行位置分配稳定编号，同一位置不重复公开。"""
    bundle = verified_quote_bundle(extracted, chosen)
    units = []
    seen = set()
    for source in bundle['sources']:
        for item in sorted(source['quotes'], key=lambda row: (row['context_span'], row['chain_span'])):
            key = (source['source_number'], tuple(item['context_span']), tuple(item['chain_span']))
            if key in seen:
                continue
            seen.add(key)
            units.append({'id': len(units) + 1, 'source_number': source['source_number'],
                          **{name: item[name] for name in ('context', 'context_span', 'chain_span', 'before', 'after')}})
    return units


def render_coverage(output, extracted, chosen, *, max_tokens, heading, boundary, empty):
    """只汇总已验收整行，不解释指代或把问句改写成断言。"""
    value = CoverageOutput.model_validate(output)
    units = coverage_units(extracted, chosen)
    if sorted(value.mandatory_ids) != list(range(1, len(units) + 1)):
        raise ValueError('answer_coverage_ids_invalid')
    numbers = [row.source_number for row in value.additional_quotes]
    if len(set(numbers)) != len(numbers) or any(n < 1 or n > len(chosen) for n in numbers):
        raise ValueError('answer_coverage_additional_sources_invalid')
    additions = {row.source_number: row.model_dump() for row in value.additional_quotes}
    extra = {'sources': [additions.get(n, {'source_number': n, 'quotes': []})
                         for n in range(1, len(chosen) + 1)]}
    combined = [*(units[index - 1] for index in value.mandatory_ids), *coverage_units(extra, chosen)]
    seen, lines, citations = set(), [], []
    for unit in combined:
        key = (unit['source_number'], tuple(unit['context_span']), tuple(unit['chain_span']))
        if key in seen:
            continue
        seen.add(key)
        number = unit['source_number']
        # 每条保持原始说话人和问尾；边界说明仅描述被选连续链，不猜省略内容。
        before = boundary if unit['before'] == 'omitted' else ''
        after = boundary if unit['after'] != 'record_end' else ''
        lines.append(f"[{number}] {before}{unit['context']}{after}")
        if number not in citations:
            citations.append(number)
    answer = heading + '\n' + '\n'.join(lines) if lines else empty
    from .budget import text_tokens
    if type(max_tokens) is not int or max_tokens <= 0 or text_tokens(answer) > max_tokens:
        raise ValueError('answer_coverage_render_limit_exceeded')
    return AskOutput(answer=answer, citations=citations)


def build_evidence_catalog(chosen, *, compose_version=None):
    """只从冻结窗口生成全部局部行目录，真缺口保持独立链。"""
    from .policies import get
    from .budget import _source_texts
    from ..context_adapter import format_recognition_content
    units, sources = [], []
    allowed = {'source_content_v1', 'document_markdown_v1', 'workspace_source_text_v1',
               'search_result_text_v1', 'recognition_experience_content_v1'}
    raw_entry_spaces = {'source_content_v1', 'recognition_content_v1',
                        'search_result_text_v1', 'recognition_experience_content_v1'}
    for number, source in enumerate(chosen, 1):
        recognition = source.get('kind') == 'recognition'
        coordinates = source.get('coordinate_space', 'recognition_content_v1' if recognition else None)
        if coordinates not in allowed | {'recognition_content_v1'} or recognition != (coordinates == 'recognition_content_v1'):
            raise ValueError('evidence_catalog_coordinate_space_unsupported')
        windows, excerpt = source.get('windows'), source.get('excerpt')
        if not isinstance(windows, (list, tuple)) or not windows or not isinstance(excerpt, str):
            raise ValueError('evidence_catalog_windows_unavailable')
        entry = source.get('entry')
        if not isinstance(entry, Mapping):
            raise ValueError('evidence_catalog_source_unavailable')
        content = entry.get('content') if coordinates in raw_entry_spaces else None
        if coordinates in raw_entry_spaces and not isinstance(content, str):
            raise ValueError('evidence_catalog_source_unavailable')
        chains, previous = [], None
        for window in windows:
            start, end, text = (getattr(window, key, None) for key in ('start', 'end', 'text'))
            if (type(start) is not int or type(end) is not int or not 0 <= start < end
                    or not isinstance(text, str) or len(text) != end - start
                    or (previous is not None and start < previous)
                    or (content is not None and (end > len(content) or text != content[start:end]))):
                raise ValueError('evidence_catalog_windows_invalid')
            if chains and chains[-1]['end'] == start:
                chains[-1]['end'] = end
                chains[-1]['text'] += text
            else:
                chains.append({'start': start, 'end': end, 'text': text})
            previous = end
        if recognition:
            if (len(windows) != 1 or windows[0].start != 0 or windows[0].end != len(content)
                    or excerpt != format_recognition_content(entry)):
                raise ValueError('evidence_catalog_recognition_invalid')
        elif excerpt != '\n…\n'.join(window.text for window in windows):
            raise ValueError('evidence_catalog_excerpt_invalid')
        # 原版依旧用真实候选计算日期，但注入的基础排版不再复制正文。
        def framing(rows, *, operation=None):
            return _source_texts([{**row, 'excerpt': ''} for row in rows])
        formatted = get('compose', version='@34' if compose_version in {'@34', '@35', '@36'} else '@16')(framing, [source], operation='sources')[0]
        formatted = formatted.replace('[1]', f'[{number}]', 1)
        metadata = {'source_number': number, 'framing_before': formatted, 'framing_after': '',
                    'windows': [[window.start, window.end] for window in windows]}
        if recognition:
            # 认识正文和条件为一个原子，来源状态属于附属元数据而不是引文。
            conditions = entry.get('conditions', ())
            atom = content + ('\n\n适用条件：\n' + '\n'.join('- ' + value for value in conditions) if conditions else '')
            metadata['recognition_provenance'] = excerpt[len(atom):]
            units.append({'id': len(units) + 1, 'source_number': number,
                'coordinate_space': coordinates, 'span': [0, len(content)], 'chain_span': [0, len(content)],
                'before': 'record_start', 'after': 'record_end', 'text': atom})
        else:
            for chain in chains:
                position = chain['start']
                for line in chain['text'].splitlines(keepends=True):
                    end = position + len(line)
                    units.append({'id': len(units) + 1, 'source_number': number,
                        'coordinate_space': coordinates, 'span': [position, end],
                        'chain_span': [chain['start'], chain['end']],
                        'before': 'record_start' if chain['start'] == 0 else 'omitted',
                        'after': ('record_end' if content is not None and chain['end'] == len(content) else 'selected_end'),
                        'text': line})
                    position = end
        sources.append(metadata)
    return {'sources': sources, 'units': units, **({'calendar_version': 3} if compose_version in {'@34', '@35', '@36'} else {})}


def model_catalog(catalog):
    """34模型投影保留每字原文、来源说明和真实缺口，不重复发送程序坐标。"""
    groups, units = {}, []
    for unit in catalog['units']:
        # 连续链身份只表示哪些原句相邻；不能因删坐标而把跨窗口两行接成原话。
        key = (unit['source_number'], tuple(unit['chain_span']))
        group = groups.setdefault(key, len(groups) + 1)
        units.append({key: unit[key] for key in ('id', 'source_number', 'before', 'after', 'text')}
            | {'continuity_group': group})
    return {'sources': [{key: source[key] for key in ('source_number', 'framing_before', 'framing_after')}
            | ({'recognition_provenance': source['recognition_provenance']} if 'recognition_provenance' in source else {})
            for source in catalog['sources']],
        'units': units}


def model_date_refs(refs):
    """只去程序校验坐标，完整原句、表达式、锚和所有合法结果仍提供给模型。"""
    return [{key: ([{name: item for name, item in option.items() if name != 'span'} for option in value]
            if key == 'options' else value)
        for key, value in ref.items() if key not in {
            'coordinate_space', 'unit_span', 'sentence_span', 'expression_span', 'published'}} for ref in refs]


def catalog_source_excerpts(catalog):
    """沿原窗口边界还原引用片段，仅供已经完成冻结证明的读模型使用。"""
    result = []
    for source in catalog['sources']:
        units = [unit for unit in catalog['units'] if unit['source_number'] == source['source_number']]
        if 'recognition_provenance' in source:
            result.append(units[0]['text'] + source['recognition_provenance'])
            continue
        pieces = []
        for start, end in source['windows']:
            parts = []
            for unit in units:
                left, right = unit['span']
                if left < end and right > start:
                    parts.append(unit['text'][max(start, left) - left:min(end, right) - left])
            piece = ''.join(parts)
            if len(piece) != end - start:
                raise ValueError('evidence_catalog_window_unavailable')
            pieces.append(piece)
        result.append('\n…\n'.join(pieces))
    return result


def validate_partition(output, catalog):
    """每个目录编号恰有一项分类，语义判断仍由模型负责。"""
    value = EvidencePartitionOutput.model_validate(output)
    numbers = [*value.selected_ids, *value.irrelevant_ids, *value.uncertain_ids]
    if sorted(numbers) != [unit['id'] for unit in catalog['units']]:
        raise ValueError('evidence_partition_ids_invalid')
    return value


def partition_output_tokens(catalog):
    """完整合法分类的序列化保守上界，供首发前动态准入。"""
    import json
    ids = [unit['id'] for unit in catalog['units']]
    # 合同要求紧凑ASCII JSON；每个ASCII字符按最多一个词元预留，避免三字符估计截断编号。
    # 分到三个非空数组最多少两个逗号；保留普通JSON空格表示和三个词元余量。
    payload = {'selected_ids': ids, 'irrelevant_ids': [], 'uncertain_ids': []}
    return len(json.dumps(payload, ensure_ascii=True)) + 3


def build_partition_messages(chosen, question, *, history='', profile=None, part_context=None, partition=None, compose_version='@18', first_output=None):
    """首末共用同一目录，每轮原文只在目录出现一次。"""
    import json
    from .policies import get
    from .budget import _ask_instruction, user_text
    from .profile import _profile_messages
    if compose_version not in {'@18', '@19', '@20', '@21', '@22', '@23', '@24', '@25', '@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        raise ValueError('invalid_partition_compose_version')
    catalog = build_evidence_catalog(chosen, compose_version=compose_version)
    if compose_version in {'@24', '@25', '@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'} and partition is not None:
        raise ValueError('first_facts_required')
    final = partition is not None or first_output is not None
    data = {'evidence_catalog': catalog}
    if compose_version in {'@24', '@25', '@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        from .policies.date_grounding import bind_date_provenance, build_date_ref_catalog
        data['date_provenance'] = bind_date_provenance(catalog, chosen) if compose_version not in {'@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'} else build_date_ref_catalog(catalog, chosen, calendar_version=3 if compose_version in {'@34', '@35', '@36'} else 2)
        if final:
            if compose_version in {'@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
                first, _, data['first_fact_diagnostics'] = inspect_referenced_first_draft(first_output, catalog, chosen=chosen, question=question, compose_version=compose_version)
            elif compose_version == '@25':
                first, data['first_fact_diagnostics'] = inspect_first_fact_draft(first_output, catalog, chosen=chosen, question=question)
            else:
                first = validate_first_facts(first_output, catalog, chosen=chosen, question=question)
            data['untrusted_first_fact_plan'] = first.model_dump()
            data['first_fact_positions'] = list(range(1, len(first.facts) + 1))
    if final and compose_version not in {'@24', '@25', '@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        data['untrusted_first_partition'] = validate_partition(partition, catalog).model_dump()
    instruction = get('compose', version=compose_version)(_ask_instruction, chosen,
        operation='partition_final_instruction' if final else 'instruction', question=question,
        **({'response_schema': final_response_model(compose_version, question).model_json_schema()}
           if compose_version in {'@21', '@22', '@23'} and final else
           {'response_schema': (final_response_model(compose_version, question) if final else answer_response_model(compose_version)).model_json_schema()} if compose_version in {'@24', '@25', '@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'} else {}),
        **({'unit_ids': tuple(unit['id'] for unit in catalog['units'])} if compose_version in {'@19', '@20', '@21', '@22', '@23', '@24', '@25', '@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'} else {}))
    if compose_version == '@31' or compose_version == '@32' and final:
        data['review_queue'] = get('compose', version=compose_version)(None, operation='review_queue',
            catalog=catalog, first_output=data.get('untrusted_first_fact_plan'),
            diagnostics=data.get('first_fact_diagnostics', ()), date_refs=data['date_provenance'])
    if compose_version == '@32' and final:
        data['first_evidence_plan'] = [{key: value for key, value in {'fact_index': index, **fact}.items()
            if key != 'text'} for index, fact in enumerate(data.pop('untrusted_first_fact_plan')['facts'], 1)]
        data = {key: data[key] for key in ('first_evidence_plan', 'first_fact_positions',
            'first_fact_diagnostics', 'review_queue', 'date_provenance', 'evidence_catalog')}
    elif compose_version in {'@29', '@30', '@31', '@33', '@34', '@35', '@36'} and final:
        # 先呈现待审草稿，真实完整资料放在问题前；嵌套值仍由冻结输入确定性重建。
        data = {key: data[key] for key in ('untrusted_first_fact_plan', 'first_fact_positions',
            'first_fact_diagnostics', *(('review_queue',) if compose_version == '@31' else ()),
            'date_provenance', 'evidence_catalog')}
    if compose_version in {'@34', '@35', '@36'}:
        # 模型只看完整正文和必要缺口；坐标与绑定仍冻结在完整目录供程序复验。
        data['evidence_catalog'] = model_catalog(catalog)
        data['date_provenance'] = model_date_refs(data['date_provenance'])
    messages = _profile_messages(profile or {}, [
        {'role': 'system', 'content': instruction},
        {'role': 'user', 'content': user_text([json.dumps(data, ensure_ascii=False, sort_keys=compose_version not in {'@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'},
            **({'separators': (',', ':')} if compose_version in {'@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'} else {}))], question, history)}])
    if part_context:
        messages.insert(1 if (profile or {}).get('text') else 0, {'role': 'user', 'content': part_context['text']})
    return messages


def render_partition(output, catalog, *, max_tokens, heading, boundary, empty, uncertain):
    """仅公开末轮选中和不确定原话，不补对象，不改变问句。"""
    from .budget import text_tokens
    value = validate_partition(output, catalog)
    selected, doubtful = set(value.selected_ids), set(value.uncertain_ids)
    lines, citations = [], []
    for unit in catalog['units']:
        if unit['id'] not in selected | doubtful:
            continue
        number = unit['source_number']
        before = boundary if unit['before'] == 'omitted' else ''
        after = boundary if unit['after'] != 'record_end' else ''
        note = uncertain if unit['id'] in doubtful else ''
        lines.append(f"[{number}] {note}{before}{unit['text']}{after}")
        if number not in citations:
            citations.append(number)
    answer = heading + '\n' + '\n'.join(lines) if lines else empty
    if type(max_tokens) is not int or max_tokens <= 0 or text_tokens(answer) > max_tokens:
        raise ValueError('evidence_partition_render_limit_exceeded')
    return AskOutput(answer=answer, citations=citations)


class StatementEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    unit_id: StrictInt
    quote: Annotated[StrictStr, Field(min_length=1)]
    role: Literal['support', 'context']


class StatementDateNote(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_number: Annotated[StrictInt, Field(ge=1)]
    text: Annotated[StrictStr, Field(min_length=1)]


class GroundedStatement(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['fact', 'inference', 'date', 'limitation']
    text: Annotated[StrictStr, Field(min_length=1)]
    evidence: Annotated[list[StatementEvidence], Field(min_length=1)]
    derived_notes: list[StatementDateNote]


class EvidenceWithdrawal(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    unit_ids: Annotated[list[StrictInt], Field(min_length=1)]
    reason: Annotated[StrictStr, Field(min_length=1)]


class GroundedAnswerOutput(BaseModel):
    # 公开仍为 AskOutput，完整分类、撤回与原引文只留在原模型结果中。
    model_config = ConfigDict(extra='forbid', strict=True)
    partition: EvidencePartitionOutput
    statements: list[GroundedStatement]
    withdrawals: list[EvidenceWithdrawal]


class EvidenceDisposition(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    unit_ids: Annotated[list[StrictInt], Field(min_length=1)]
    status: Literal['unused', 'uncertain']
    reason: Annotated[StrictStr, Field(min_length=1)]


class GroundedUsageAnswerOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    statements: list[GroundedStatement]
    dispositions: list[EvidenceDisposition]


class UnreferencedEvidenceScope(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    scope: Literal['all_unreferenced']
    reason: Annotated[StrictStr, Field(min_length=1)]


class GroundedScopeAnswerOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    statements: list[GroundedStatement]
    unused: UnreferencedEvidenceScope
    uncertain: list[EvidenceWithdrawal]


class FirstFactsOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    partition: EvidencePartitionOutput
    facts: list[GroundedStatement]


class FirstFactUse(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    fact_index: Annotated[StrictInt, Field(ge=1)]
    action: Literal['adopt', 'merge', 'correct']
    reason: StrictStr


class ReviewedStatement(GroundedStatement):
    first_fact_uses: list[FirstFactUse]


class ExcludedFacts(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    fact_indices: Annotated[list[Annotated[StrictInt, Field(ge=1)]], Field(min_length=1)]
    reason: Annotated[StrictStr, Field(min_length=1)]


class ReviewedScopeAnswerOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    statements: list[ReviewedStatement]
    unused: UnreferencedEvidenceScope
    uncertain: list[EvidenceWithdrawal]
    excluded_facts: list[ExcludedFacts]



class ReferencedStatement(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['fact', 'inference', 'date', 'limitation']
    text: Annotated[StrictStr, Field(min_length=1)]
    evidence: Annotated[list[StatementEvidence], Field(min_length=1)]
    date_refs: list[Annotated[StrictInt, Field(ge=1)]]


class FirstReferencedFactsOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    facts: list[ReferencedStatement]
    unreviewed: UnreferencedEvidenceScope


class ReviewedReferencedStatement(ReferencedStatement):
    first_fact_uses: list[FirstFactUse]


class ReviewedReferencedScopeOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    statements: list[ReviewedReferencedStatement]
    unused: UnreferencedEvidenceScope
    uncertain: list[EvidenceWithdrawal]
    excluded_facts: list[ExcludedFacts]


class FocusedReferencedFactsOutput(FirstReferencedFactsOutput):
    # 新私有Schema保持字段不变，旧26的精确Schema与冻结消息不受影响。
    facts: Annotated[list[ReferencedStatement], Field(json_schema_extra={'uniqueItems': True},
        description='Minimal sufficient question-directed propositions, each once. Stop after the requested information is covered; do not summarize sources.')]


class FocusedReviewedScopeOutput(ReviewedReferencedScopeOutput):
    statements: Annotated[list[ReviewedReferencedStatement], Field(json_schema_extra={'uniqueItems': True},
        description='Concise final requested facts or meanings, each once; no repeated recap. Actually repair diagnosed kind/date_refs or exclude that first position.')]


class LocalSourceClause(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    unit_id: StrictInt
    quote: Annotated[StrictStr, Field(min_length=1)]


class ReconstructedStatement(ReviewedReferencedStatement):
    stance: Literal['asserted', 'planned', 'missed', 'conditional', 'question', 'inference', 'unknown']
    source_clauses: list[LocalSourceClause]


class ReviewDisposition(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    item_ids: Annotated[list[Annotated[StrictInt, Field(ge=1)]], Field(min_length=1)]
    action: Literal['include', 'exclude']
    statement_indices: list[Annotated[StrictInt, Field(ge=1)]]
    reason: Annotated[StrictStr, Field(min_length=1, max_length=240)]


class ReconstructedReviewedOutput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    statements: list[ReconstructedStatement]
    unused: UnreferencedEvidenceScope
    uncertain: list[EvidenceWithdrawal]
    excluded_facts: list[ExcludedFacts]
    review_dispositions: list[ReviewDisposition]


def first_fact_partition(output, catalog, *, compose_version='@24'):
    if compose_version not in {'@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        return output['partition']
    # 仅供旧末guard计算撤回的内部资格集合；补集是未引用/未审核，绝非模型无关判断。
    # 此view不发送或写回首原件，也不限制末轮使用目录中的其它证据。
    first = FirstReferencedFactsOutput.model_validate(output)
    if not first.unreviewed.reason.strip():
        raise ValueError('first_unreviewed_reason_empty')
    used = {item.unit_id for fact in first.facts for item in fact.evidence}
    complete = {unit['id'] for unit in catalog['units']}
    if compose_version in {'@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        # 首稿未知单元只有诊断资格，内部预选不能把它投影为有效证据。
        used &= complete
    if not used <= complete:
        raise ValueError('grounded_evidence_invalid')
    return {'selected_ids': sorted(used), 'irrelevant_ids': sorted(complete - used), 'uncertain_ids': []}


def first_fact_empty(compose_version, catalog):
    if compose_version in {'@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        return {'facts': [], 'unreviewed': {'scope': 'all_unreferenced', 'reason': 'No first facts have been generated; all evidence remains unreviewed.'}}
    return {'partition': {'selected_ids': [unit['id'] for unit in catalog['units']],
        'irrelevant_ids': [], 'uncertain_ids': []}, 'facts': []}


def _resolve_reference_statements(statements, catalog, chosen, *, compose_version='@26'):
    from .policies.date_grounding import build_date_ref_catalog, resolve_date_refs
    refs = build_date_ref_catalog(catalog, chosen, calendar_version=3 if compose_version in {'@34', '@35', '@36'} else 2)
    normalized, bindings = [], {}
    for statement in statements:
        records, notes = resolve_date_refs(statement, catalog, chosen, refs, compose_version=compose_version)
        value = GroundedStatement.model_validate({**statement.model_dump(exclude={'date_refs', 'first_fact_uses'}), 'derived_notes': notes})
        normalized.append(value)
        bindings[id(value)] = records
    return normalized, bindings


def inspect_referenced_first_draft(output, catalog, *, chosen, question, compose_version='@26'):
    if compose_version in {'@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        return _inspect_repairable_first_draft(output, catalog, chosen=chosen, question=question, compose_version=compose_version)
    from .policies.date_grounding import validate_calendar_statements_v1, validate_calendar_statements_v2
    raw = (FocusedReferencedFactsOutput if compose_version in {'@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'} else FirstReferencedFactsOutput).model_validate(output)
    partition = first_fact_partition(raw.model_dump(), catalog, compose_version=compose_version)
    statements, bindings = _resolve_reference_statements(raw.facts, catalog, chosen, compose_version=compose_version)
    normalized = FirstFactsOutput.model_validate({'facts': statements, 'partition': partition})
    calendar_validate = validate_calendar_statements_v2 if compose_version in {'@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'} else validate_calendar_statements_v1
    _, diagnostics = inspect_first_fact_draft(normalized, catalog, chosen=chosen, question=question,
        _calendar_validate=lambda values, cat, rows, text: calendar_validate(
            values, cat, rows, text, record_bindings=bindings))
    if compose_version in {'@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        codes = {item['fact_index']: list(item['codes']) for item in diagnostics}
        seen = set()
        for index, statement in enumerate(raw.facts, 1):
            if compose_version == '@27' and statement.kind in {'fact', 'inference'} and statement.date_refs:
                codes.setdefault(index, []).insert(0, 'grounded_date_ref_kind_invalid')
            key = _referenced_statement_key(statement, compose_version=compose_version)
            if key in seen:
                codes.setdefault(index, []).append('grounded_statement_duplicate')
            seen.add(key)
        diagnostics = [{'fact_index': index, 'codes': errors} for index, errors in sorted(codes.items())]
    return raw, normalized, diagnostics


def _first_field_issues(statement, catalog, code):
    """只解释已被共同守卫拒绝的模型字段，不增加资格或猜改原件。"""
    units = {unit['id']: unit for unit in catalog['units']}
    if code in {'grounded_evidence_invalid', 'first_fact_outside_partition'}:
        issues, seen = [], set()
        for index, item in enumerate(statement.evidence):
            unit = units.get(item.unit_id)
            if unit is None:
                issues.append({'field_path': f'evidence[{index}].unit_id', 'condition': 'unknown_catalog_unit',
                    'repair': 'Use a real catalog unit and its verbatim quote, or exclude this first position.'})
            elif ((item.unit_id, item.quote) in seen or not item.quote.strip()
                    or DERIVED_NOTES in item.quote or item.quote not in unit['text']):
                issues.append({'field_path': f'evidence[{index}].quote', 'condition': 'not_unique_verbatim_unit_text',
                    'unit_id': item.unit_id, 'coordinate_space': unit['coordinate_space'], 'unit_span': unit['span'],
                    'repair': 'Replace with unique continuous raw text in this unit; titles and program notes are not unit quotes, or exclude.'})
            seen.add((item.unit_id, item.quote))
        if issues:
            return issues
    if code == 'grounded_statement_without_support':
        return [{'field_path': 'evidence[*].role', 'condition': 'positive_claim_has_no_support',
            'actual_roles': [item.role for item in statement.evidence],
            'unit_ids': [item.unit_id for item in statement.evidence],
            'repair': 'Use support only for quotes that substantiate a positive claim. For an unknown answer, actually change kind to limitation and narrow text; otherwise exclude.'}]
    return [{'field_path': 'text', 'condition': code,
        'repair': 'Actually repair the identified text/kind/evidence or exclude this first position; a reason alone changes no field.'}]


def _inspect_repairable_first_draft(output, catalog, *, chosen, question, compose_version='@29'):
    """29首稿仍不可信；模型字段错可审，冻结资料损坏和程序异常仍终止。"""
    from .policies import get
    from .policies.date_grounding import build_date_ref_catalog, resolve_date_refs, validate_calendar_statements_v3, validate_calendar_statements_v4, validate_calendar_statements_v5, validate_calendar_statements_v6, validate_calendar_statements_v7, _computed_match, _frozen_record_anchor
    if build_evidence_catalog(chosen, compose_version=compose_version) != catalog:
        raise ValueError('first_frozen_catalog_invalid')
    raw = FocusedReferencedFactsOutput.model_validate(output)
    partition = first_fact_partition(raw.model_dump(), catalog, compose_version=compose_version)
    refs = build_date_ref_catalog(catalog, chosen, calendar_version=3 if compose_version in {'@34', '@35', '@36'} else 2)
    statements, bindings, codes, issues = [], {}, {}, {}
    allowed = get('compose', version=compose_version)(None, operation='first_fact_error_codes')
    def record(index, code, details):
        if code not in codes.setdefault(index, []):
            codes[index].append(code)
        for detail in details:
            issues.setdefault(index, []).append({'code': code, **detail,
                'field_path': f'facts[{index - 1}].' + detail['field_path']})
    for index, original in enumerate(raw.facts, 1):
        ref_issues = []
        try:
            records, notes = resolve_date_refs(original, catalog, chosen, refs, compose_version=compose_version, _on_invalid=ref_issues.append)
        except ValueError as error:
            if str(error) != 'grounded_date_ref_invalid':
                raise
            record(index, str(error), ref_issues)
            records, notes = [], []
        statement = GroundedStatement.model_validate({**original.model_dump(exclude={'date_refs'}), 'derived_notes': notes})
        statements.append(statement)
        bindings[id(statement)] = records
    accepted = []
    for offset in sorted(range(len(statements)), key=lambda position: statements[position].kind == 'limitation'):
        index, statement = offset + 1, statements[offset]
        calendar_issues = []
        def on_calendar_error(item, value):
            matching_refs, value_refs = [], []
            original = raw.facts[offset]
            for ref in refs:
                if not any(evidence.unit_id == ref['unit_id'] for evidence in original.evidence):
                    continue
                try:
                    resolve_date_refs(original.model_copy(update={'kind': 'date', 'date_refs': [ref['date_ref_id']]}),
                        catalog, chosen, refs, compose_version=compose_version)
                except ValueError as error:
                    if str(error) != 'grounded_date_ref_invalid':
                        raise
                else:
                    matching_refs.append(ref['date_ref_id'])
                    if _computed_match(value, ref['options']):
                        value_refs.append(ref['date_ref_id'])
            detail = {'field_path': 'text', 'condition': 'calendar_value_not_authorized_by_cited_evidence',
                'text_span': list(value['span']), 'calendar_value': {key: value[key] for key in ('start', 'end', 'precision')},
                'matching_date_ref_ids': value_refs if compose_version in {'@34', '@35', '@36'} else matching_refs,
                **({'same_sentence_date_ref_ids': matching_refs,
                    'record_date_context': [{'unit_id': evidence.unit_id, 'record_date': anchor}
                        for evidence in original.evidence
                        if (anchor := _frozen_record_anchor(chosen[next(unit['source_number'] for unit in catalog['units'] if unit['id'] == evidence.unit_id) - 1])) is not None]}
                    if compose_version in {'@34', '@35', '@36'} else {}),
                **({'actual_kind': original.kind, 'date_refs_grant_calculation': original.kind == 'date'} if compose_version in {'@30', '@31', '@32', '@33', '@34', '@35', '@36'} else {}),
                'repair': ('Actually remove unsupported dates or explicitly label a verified same-source record anchor. '
                    'Computed event dates need kind=date, full sentence and legal result. Merge into a valid date with all first positions accounted for, or exclude; reason alone changes nothing.'
                    if compose_version in {'@30', '@31', '@32', '@33', '@34', '@35', '@36'} else 'Remove the unsupported calendar text, not merely date_refs; or use kind=date with a real matching full-sentence ref and legal full precision, or exclude.')}
            if detail not in calendar_issues:
                calendar_issues.append(detail)
        def calendar_validate(values, cat, rows, text):
            return (validate_calendar_statements_v7 if compose_version == '@36' else validate_calendar_statements_v6 if compose_version in {'@34', '@35', '@36'} else validate_calendar_statements_v5 if compose_version == '@33' else validate_calendar_statements_v4 if compose_version in {'@30', '@31', '@32'} else validate_calendar_statements_v3)([*accepted, *values], cat, rows, text,
                record_bindings=bindings, _on_invalid=on_calendar_error,
                **({'_collect_invalid': True} if compose_version in {'@34', '@35', '@36'} else {}))
        try:
            _, found = inspect_first_fact_draft({'facts': [statement], 'partition': partition},
                catalog, chosen=chosen, question=question, _calendar_validate=calendar_validate)
            local_codes = found[0]['codes'] if found else []
        except ValueError as error:
            if str(error) not in allowed:
                raise
            local_codes = [str(error)]
        for code in local_codes:
            record(index, code, calendar_issues if code == 'grounded_calendar_provenance_invalid'
                else _first_field_issues(statement, catalog, code))
        if index in codes:
            # 无效首条不能给后面的limitation或任何日期值提供正面计算授权。
            statements[offset] = statement.model_copy(update={'derived_notes': []})
            bindings[id(statements[offset])] = []
        elif statement.kind != 'limitation':
            accepted.append(statement)
    seen = {}
    for index, statement in enumerate(raw.facts, 1):
        key = _referenced_statement_key(statement, compose_version=compose_version)
        if key in seen:
            record(index, 'grounded_statement_duplicate', [{'field_path': 'text', 'condition': 'complete_duplicate',
                'duplicate_of': seen[key], 'repair': 'Keep a single final statement; actually correct this duplicate position or exclude it, accounting for every first position once.'}])
        else:
            seen[key] = index
    normalized = FirstFactsOutput.model_validate({'facts': statements, 'partition': partition})
    return raw, normalized, [{'fact_index': index, 'codes': errors, 'issues': issues[index]}
        for index, errors in sorted(codes.items())]


def _referenced_statement_key(statement, *, compose_version='@26'):
    # 28的普通声明附带ref没有计算资格，不能靠改编号让完整重复看似不同。
    refs = [] if compose_version in {'@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'} and statement.kind in {'fact', 'inference'} else statement.date_refs
    return (statement.kind, statement.text.strip(), frozenset((item.unit_id, item.quote, item.role)
        for item in statement.evidence), frozenset(refs))


def reviewed_referenced_answer(output, catalog, first_output, *, chosen, question, compose_version='@26'):
    if compose_version == '@32':
        return reviewed_reconstructed_answer(output, catalog, first_output, chosen=chosen, question=question)
    from .policies.date_grounding import validate_calendar_statements_v1, validate_calendar_statements_v2, validate_calendar_statements_v3, validate_calendar_statements_v4, validate_calendar_statements_v5, validate_calendar_statements_v6, validate_calendar_statements_v7
    _, first, diagnostics = inspect_referenced_first_draft(first_output, catalog, chosen=chosen,
        question=question, compose_version=compose_version)
    value = (FocusedReviewedScopeOutput if compose_version in {'@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'} else ReviewedReferencedScopeOutput).model_validate(output)
    invalid = {item['fact_index'] for item in diagnostics}
    seen = set()
    for statement in value.statements:
        # 同引文可以表达不同含义；只拒绝完整相同声明，不自动删改模型原件。
        key = _referenced_statement_key(statement, compose_version=compose_version)
        if key in seen:
            raise ValueError('grounded_statement_duplicate')
        seen.add(key)
        for use in statement.first_fact_uses:
            if not use.reason.strip():
                raise ValueError('first_fact_review_reason_empty')
            if use.fact_index in invalid and use.action != 'correct':
                raise ValueError('invalid_first_fact_requires_correction')
        if compose_version == '@27' and statement.kind in {'fact', 'inference'} and statement.date_refs:
            # reason里的“已改正”不能替代实际字段；不猜改kind，也不悄悄清空refs。
            raise ValueError('grounded_date_ref_kind_invalid')
    statements, bindings = _resolve_reference_statements(value.statements, catalog, chosen, compose_version=compose_version)
    # 先核所有最终出处与引用，再按每条明示ref重验日期，不能借同源另一计算。
    normalized = ReviewedScopeAnswerOutput.model_validate({**value.model_dump(exclude={'statements'}),
        'statements': [{**statement.model_dump(), 'first_fact_uses': raw.first_fact_uses}
            for statement, raw in zip(statements, value.statements)]})
    if compose_version in {'@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        # 旧25会再用整份来源注释/v1复审，造成新ref合同被旧锚规则误拦；共用原处置核验后只跑本版日期核验。
        return _reviewed_scope_answer(normalized, catalog, first, invalid, chosen=chosen, question=question,
            calendar_validate=lambda values, cat, rows, text: (validate_calendar_statements_v7 if compose_version == '@36' else validate_calendar_statements_v6 if compose_version in {'@34', '@35', '@36'} else validate_calendar_statements_v5 if compose_version == '@33' else validate_calendar_statements_v4 if compose_version in {'@30', '@31', '@32'} else validate_calendar_statements_v3 if compose_version == '@29' else validate_calendar_statements_v2)(values, cat, rows, text,
                record_bindings={id(item): bindings[id(original)] for item, original in zip(values, statements)}))
    result = reviewed_scope_answer(normalized, catalog, first.model_dump(), chosen=chosen, question=question, compose_version='@25')
    # 旧mapper重建了实例，重新将当前声明的真实refs绑定给对应结果实例。
    rebound = {id(statement): bindings[id(original)] for statement, original in zip(result.statements, statements)}
    validate_calendar_statements_v1(result.statements, catalog, chosen, question, record_bindings=rebound)
    return result

def inspect_first_fact_draft(output, catalog, *, chosen, question, _calendar_validate=None):
    """25先全量核硬边界，再给可纠正草稿错误生成程序诊断；不改原响应。"""
    from .policies import get
    from .policies.date_grounding import validate_calendar_statements_v1
    value = FirstFactsOutput.model_validate(output)
    first = validate_partition(value.partition, catalog)
    selected, uncertain = set(first.selected_ids), set(first.uncertain_ids)
    units = {unit['id']: unit for unit in catalog['units']}
    dates = frozen_date_notes(catalog, chosen)
    # 硬前置必须扫完所有facts，不能被前面的软错误掩盖后面伪造的引文或注释。
    for statement in value.facts:
        if not statement.text.strip():
            raise ValueError('grounded_statement_empty')
        seen, sources = set(), set()
        for item in statement.evidence:
            unit = units.get(item.unit_id)
            key = (item.unit_id, item.quote)
            if item.unit_id not in selected | uncertain:
                raise ValueError('first_fact_outside_partition')
            if (unit is None or key in seen or not item.quote.strip() or DERIVED_NOTES in item.quote
                    or item.quote not in unit['text']):
                raise ValueError('grounded_evidence_invalid')
            seen.add(key)
            sources.add(unit['source_number'])
        seen_notes = set()
        for note in statement.derived_notes:
            if (note.source_number in seen_notes or note.source_number not in sources
                    or dates.get(note.source_number) != note.text):
                raise ValueError('grounded_date_note_invalid')
            seen_notes.add(note.source_number)
        grounded_statement_text(statement, units)
    calendar_validate = _calendar_validate or validate_calendar_statements_v1
    soft = get('compose', version='@25')(None, operation='first_fact_error_codes')
    diagnostics = [{'fact_index': index, 'status': 'valid', 'error_codes': []}
        for index in range(1, len(value.facts) + 1)]
    def diagnose(index, operation):
        try:
            operation()
        except ValueError as error:
            code = str(error)
            if code not in soft:
                raise
            diagnostics[index]['status'] = 'invalid'
            diagnostics[index]['error_codes'].append(code)
    for index, statement in enumerate(value.facts):
        if statement.kind != 'limitation' and any(item.unit_id in uncertain for item in statement.evidence):
            diagnostics[index]['status'] = 'invalid'
            diagnostics[index]['error_codes'].append('first_fact_uncertain_kind_invalid')
            continue
        used = {item.unit_id for item in statement.evidence}
        view = {'selected_ids': sorted(used & selected), 'uncertain_ids': sorted(used & uncertain),
            'irrelevant_ids': sorted(set(units) - used)}
        diagnose(index, lambda statement=statement, view=view: validate_grounded_answer(
            {'partition': view, 'statements': [statement.model_dump()], 'withdrawals': []}, catalog, view, chosen=chosen))
    accepted = []
    # 整条正面声明通过全部日期检查后才可授权后续精度说明，局部成功不进入集合。
    order = sorted(range(len(value.facts)), key=lambda index: value.facts[index].kind == 'limitation')
    for index in order:
        if diagnostics[index]['status'] == 'invalid':
            continue
        statement = value.facts[index]
        diagnose(index, lambda statement=statement: calendar_validate(
            [*accepted, statement], catalog, chosen, question))
        if diagnostics[index]['status'] == 'valid' and statement.kind != 'limitation':
            accepted.append(statement)
    return value, [{'fact_index': item['fact_index'], 'codes': item['error_codes']}
        for item in diagnostics if item['status'] == 'invalid']


def first_fact_diagnostics(output, catalog, *, chosen, question):
    return inspect_first_fact_draft(output, catalog, chosen=chosen, question=question)[1]


def validate_first_facts(output, catalog, *, chosen, question, compose_version='@24'):
    """首分类是预选，不要求每个预选单元都被首要点引用。"""
    from .policies.date_grounding import validate_calendar_statements_v1
    if compose_version in {'@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        return inspect_referenced_first_draft(output, catalog, chosen=chosen, question=question, compose_version=compose_version)[0]
    if compose_version == '@25':
        return inspect_first_fact_draft(output, catalog, chosen=chosen, question=question)[0]
    value = FirstFactsOutput.model_validate(output)
    partition = validate_partition(value.partition, catalog)
    selected, uncertain = set(partition.selected_ids), set(partition.uncertain_ids)
    complete = {unit['id'] for unit in catalog['units']}
    used = {item.unit_id for fact in value.facts for item in fact.evidence}
    if not used <= selected | uncertain:
        raise ValueError('first_fact_outside_partition')
    # 仅用于复用语句守卫的精确引用视图，无撤回或虚构unused理由。
    view = {'selected_ids': sorted(used & selected), 'uncertain_ids': sorted(used & uncertain),
            'irrelevant_ids': sorted(complete - used)}
    validate_grounded_answer({'partition': view, 'statements': [fact.model_dump() for fact in value.facts],
        'withdrawals': []}, catalog, view, chosen=chosen)
    validate_calendar_statements_v1(value.facts, catalog, chosen, question)
    return value


def reviewed_scope_answer(output, catalog, first_output, *, chosen, question, compose_version='@24', _on_projection=None):
    """逐位置审核首要点，原件经共同守卫后才映射为23采用账本。"""
    from .policies.date_grounding import validate_calendar_statements_v1
    if compose_version in {'@35', '@36'}:
        from .calendar_review_projection import project_review
        projected, report = project_review(output, catalog, first_output, chosen=chosen,
            question=question, compose_version=compose_version)
        result = reviewed_referenced_answer(projected, catalog, first_output, chosen=chosen,
            question=question, compose_version='@36' if compose_version == '@36' else '@34')
        if _on_projection is not None:
            _on_projection(report)
        return result
    if compose_version in {'@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        return reviewed_referenced_answer(output, catalog, first_output, chosen=chosen, question=question, compose_version=compose_version)
    if compose_version == '@25':
        first, diagnostics = inspect_first_fact_draft(first_output, catalog, chosen=chosen, question=question)
        invalid = {item['fact_index'] for item in diagnostics}
    else:
        first = validate_first_facts(first_output, catalog, chosen=chosen, question=question)
        invalid = set()
    value = ReviewedScopeAnswerOutput.model_validate(output)
    return _reviewed_scope_answer(value, catalog, first, invalid, chosen=chosen, question=question,
        calendar_validate=validate_calendar_statements_v1)


def _reviewed_scope_answer(value, catalog, first, invalid, *, chosen, question, calendar_validate):
    """各版共用逐位置处置和出处守卫，日历语义由实际冻结版本提供。"""
    positions = []
    for statement in value.statements:
        for use in statement.first_fact_uses:
            positions.append(use.fact_index)
            if not 1 <= use.fact_index <= len(first.facts):
                raise ValueError('first_fact_position_invalid')
            if use.fact_index in invalid and use.action != 'correct':
                raise ValueError('invalid_first_fact_requires_correction')
            if use.action == 'correct':
                if not use.reason.strip():
                    raise ValueError('first_fact_correction_reason_empty')
            else:
                for original in first.facts[use.fact_index - 1].evidence:
                    if not any(item.unit_id == original.unit_id and original.quote in item.quote
                            and (original.role != 'support' or item.role == 'support')
                            for item in statement.evidence):
                        raise ValueError('first_fact_adoption_quote_missing')
    for group in value.excluded_facts:
        if not group.reason.strip():
            raise ValueError('excluded_fact_reason_empty')
        positions.extend(group.fact_indices)
    if len(set(positions)) != len(positions) or set(positions) != set(range(1, len(first.facts) + 1)):
        raise ValueError('first_fact_review_coverage_invalid')
    stripped = {'statements': [item.model_dump(exclude={'first_fact_uses'}) for item in value.statements],
        'unused': value.unused.model_dump(), 'uncertain': [item.model_dump() for item in value.uncertain]}
    normalized = grounded_scope_answer(stripped, catalog, first.partition, chosen=chosen)
    calendar_validate(normalized.statements, catalog, chosen, question)
    return normalized


def grounded_scope_answer(output, catalog, first_partition, *, chosen=None):
    """23按模型明示范围计算未采用集合，整体原则不冒充逐条审查证据。"""
    value = GroundedScopeAnswerOutput.model_validate(output)
    first = validate_partition(first_partition, catalog)
    complete = {unit['id'] for unit in catalog['units']}
    evidence = {item.unit_id for statement in value.statements for item in statement.evidence}
    if not evidence <= complete or not value.unused.reason.strip():
        raise ValueError('grounded_unused_scope_invalid')
    uncertain = set()
    for group in value.uncertain:
        ids = set(group.unit_ids)
        if (not group.reason.strip() or len(ids) != len(group.unit_ids)
                or ids & uncertain or not ids <= evidence):
            raise ValueError('grounded_scope_uncertain_invalid')
        uncertain.update(ids)
    unused, selected = complete - evidence, evidence - uncertain
    withdrawn = (set(first.selected_ids) - selected) | (set(first.uncertain_ids) & unused)
    withdrawals = ([{'unit_ids': sorted(withdrawn & unused), 'reason': value.unused.reason}]
        if withdrawn & unused else [])
    withdrawals.extend({'unit_ids': [item for item in group.unit_ids if item in withdrawn], 'reason': group.reason}
        for group in value.uncertain if set(group.unit_ids) & withdrawn)
    normalized = {'partition': {'selected_ids': sorted(selected), 'irrelevant_ids': sorted(unused),
        'uncertain_ids': sorted(uncertain)}, 'statements': [item.model_dump() for item in value.statements],
        'withdrawals': withdrawals}
    return validate_grounded_answer(normalized, catalog, first, chosen=chosen)


def grounded_usage_answer(output, catalog, first_partition, *, chosen=None):
    """22只从实际引用和模型显式处置计算分类，编号或理由缺失不能由程序补全。"""
    value = GroundedUsageAnswerOutput.model_validate(output)
    first = validate_partition(first_partition, catalog)
    complete = {unit['id'] for unit in catalog['units']}
    evidence = {item.unit_id for statement in value.statements for item in statement.evidence}
    unused, uncertain, seen = set(), set(), set()
    for disposition in value.dispositions:
        ids = set(disposition.unit_ids)
        if (not disposition.reason.strip() or len(ids) != len(disposition.unit_ids)
                or ids & seen or not ids <= complete):
            raise ValueError('grounded_dispositions_invalid')
        seen.update(ids)
        (unused if disposition.status == 'unused' else uncertain).update(ids)
    if evidence & unused or evidence | unused != complete or not uncertain <= evidence:
        raise ValueError('grounded_disposition_coverage_invalid')
    selected = evidence - uncertain
    withdrawn = (set(first.selected_ids) - selected) | (set(first.uncertain_ids) & unused)
    # 原模型理由按精确差集映射，不能为未列出的编号发明默认理由。
    withdrawals = [{'unit_ids': [item for item in group.unit_ids if item in withdrawn], 'reason': group.reason}
        for group in value.dispositions if set(group.unit_ids) & withdrawn]
    normalized = {'partition': {'selected_ids': sorted(selected), 'irrelevant_ids': sorted(unused),
        'uncertain_ids': sorted(uncertain)}, 'statements': [item.model_dump() for item in value.statements],
        'withdrawals': withdrawals}
    return validate_grounded_answer(normalized, catalog, first, chosen=chosen)


def frozen_date_notes(catalog, chosen=None):
    """从同轮真实候选按冻结目录版本复算注释，标题同名文字不能冒充计算。"""
    from .policies.date_calculation import append_date_notes
    if chosen is None:
        return {}
    if len(chosen) != len(catalog['sources']):
        raise ValueError('grounded_date_sources_invalid')
    result = {}
    for number, (source, candidate) in enumerate(zip(catalog['sources'], chosen), 1):
        text = append_date_notes('', candidate, quote_version=3, calendar_version=catalog.get('calendar_version', 2)).lstrip('\n')
        if text:
            if source['source_number'] != number or not source['framing_before'].endswith(text):
                raise ValueError('grounded_date_sources_invalid')
            result[number] = text
    return result


def validate_grounded_answer(output, catalog, first_partition, *, chosen=None):
    """验证出处和编号处置完整；不声称证明概括的语义蕴含。"""
    value = GroundedAnswerOutput.model_validate(output)
    final = validate_partition(value.partition, catalog)
    first = validate_partition(first_partition, catalog)
    selected, uncertain = set(final.selected_ids), set(final.uncertain_ids)
    units = {unit['id']: unit for unit in catalog['units']}
    covered, limited = set(), set()
    dates = frozen_date_notes(catalog, chosen)
    for statement in value.statements:
        if not statement.text.strip():
            raise ValueError('grounded_statement_empty')
        seen, sources = set(), set()
        for item in statement.evidence:
            unit = units.get(item.unit_id)
            key = (item.unit_id, item.quote)
            if (unit is None or key in seen or not item.quote.strip() or DERIVED_NOTES in item.quote
                    or item.quote not in unit['text'] or item.unit_id not in selected | uncertain
                    or item.unit_id in uncertain and statement.kind != 'limitation'):
                raise ValueError('grounded_evidence_invalid')
            seen.add(key)
            sources.add(unit['source_number'])
            covered.add(item.unit_id)
            if statement.kind == 'limitation':
                limited.add(item.unit_id)
        if statement.kind != 'limitation' and not any(item.role == 'support' for item in statement.evidence):
            raise ValueError('grounded_statement_without_support')
        if statement.kind != 'date' and statement.derived_notes:
            raise ValueError('grounded_date_note_kind_invalid')
        seen_notes = set()
        for note in statement.derived_notes:
            if (note.source_number in seen_notes or note.source_number not in sources
                    or dates.get(note.source_number) != note.text):
                raise ValueError('grounded_date_note_invalid')
            seen_notes.add(note.source_number)
        grounded_statement_text(statement, units)
    if not selected <= covered or not uncertain <= limited:
        raise ValueError('grounded_partition_coverage_missing')
    withdrawn = []
    for group in value.withdrawals:
        if not group.reason.strip():
            raise ValueError('grounded_withdrawal_reason_empty')
        withdrawn.extend(group.unit_ids)
    expected = (set(first.selected_ids) - selected) | (set(first.uncertain_ids) & set(final.irrelevant_ids))
    if len(withdrawn) != len(set(withdrawn)) or set(withdrawn) != expected:
        raise ValueError('grounded_withdrawals_invalid')
    return value


def grounded_statement_text(statement, units):
    """原话中的数字标记按字面量显示，其余引用只能由本声明实际出处生成。"""
    import re
    pattern = re.compile(r'\[([0-9]+)\]|【([0-9]+)】')
    text = statement.text
    sources = {units[item.unit_id]['source_number'] for item in statement.evidence}
    literal_spans = []
    for item in statement.evidence:
        start = 0
        while (position := text.find(item.quote, start)) >= 0:
            literal_spans.append((position, position + len(item.quote)))
            start = position + 1
    def replace(match):
        digits = match.group(1) or match.group(2)
        if any(start <= match.start() and match.end() <= end for start, end in literal_spans):
            return f'［{digits}］'
        # 相同原话标记存在但无法定位完整引文时，不能猜测它是字面量还是来源号。
        if any(match.group() in item.quote for item in statement.evidence):
            raise ValueError('grounded_statement_citation_ambiguous')
        if int(digits) not in sources:
            raise ValueError('grounded_statement_citation_invalid')
        return ''
    result = pattern.sub(replace, text).strip()
    if not result:
        raise ValueError('grounded_statement_empty')
    return result


def first_facts_output_minimum(catalog, *, compose_version='@24'):
    """各版本真实首外壳的容量下界；事实内容仍须首后实际预算审核。"""
    if compose_version in {'@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        return len('{"facts":[],"unreviewed":{"scope":"all_unreferenced","reason":"x"}}') + 3
    return partition_output_tokens(catalog) + len('{"partition":{},"facts":[]}')


def grounded_output_minimum(catalog, *, compose_version='@20', review_queue=None):
    """分类全集加最小外壳的保守容量下界；不承诺任意声明都能装入。"""
    if compose_version in {'@23', '@24', '@25', '@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
        import json
        shell = {'statements': [], 'unused': {'scope': 'all_unreferenced', 'reason': 'x'}, 'uncertain': []}
        if compose_version in {'@24', '@25', '@26', '@27', '@28', '@29', '@30', '@31', '@32', '@33', '@34', '@35', '@36'}:
            shell['excluded_facts'] = []
        if compose_version == '@32':
            from .policies import get
            queue = review_queue if review_queue is not None else get('compose', version='@32')(
                None, operation='review_queue', catalog=catalog)
            shell['review_dispositions'] = [{'item_ids': [row['item_id'] for row in queue['items']],
                'action': 'exclude', 'statement_indices': [], 'reason': 'x'}] if queue['items'] else []
        return len(json.dumps(shell, ensure_ascii=True)) + 3
    if compose_version == '@22':
        import json
        ids = [unit['id'] for unit in catalog['units']]
        shell = {'statements': [], 'dispositions': [{'unit_ids': ids, 'status': 'unused', 'reason': 'x'}] if ids else []}
        return len(json.dumps(shell, ensure_ascii=True)) + 3
    return partition_output_tokens(catalog) + len('{"partition":{},"statements":[],"withdrawals":[]}')


def grounded_output_limit(plan, input_tokens):
    """使用本轮冻结网关预算声明，不把它称为第三方能力实测。"""
    budget = plan.get('answer_output_budget')
    if (not isinstance(budget, dict) or set(budget) != {'window', 'reserve', 'target_revision'}
            or any(type(budget.get(key)) is not int for key in ('window', 'reserve'))
            or not 0 < budget['reserve'] < budget['window']
            or type(budget['target_revision']) is not int or type(plan['target']['revision']) is not int
            or budget['target_revision'] != plan['target']['revision']
            or type(input_tokens) is not int or input_tokens < 0):
        raise ValueError('grounded_output_budget_unavailable')
    result = min(budget['reserve'], budget['window'] - input_tokens)
    if result <= 0:
        raise ValueError('grounded_output_budget_exceeded')
    return result


def render_grounded_answer(output, catalog, first_partition, *, max_output_tokens, unknown, inference, limitation, chosen=None):
    """仅公开逐条声明和自动来源号；原引文与撤回留在私有不可变结果中。"""
    from .budget import text_tokens
    value = validate_grounded_answer(output, catalog, first_partition, chosen=chosen)
    units = {unit['id']: unit for unit in catalog['units']}
    lines, citations = [], []
    positive = any(item.kind != 'limitation' for item in value.statements)
    if not positive:
        lines.append(unknown)
    for item in value.statements:
        numbers = list(dict.fromkeys(units[evidence.unit_id]['source_number'] for evidence in item.evidence))
        label = inference if item.kind == 'inference' else limitation if item.kind == 'limitation' else ''
        lines.append(label + grounded_statement_text(item, units) + ' ' + ' '.join(f'[{number}]' for number in numbers))
        citations.extend(number for number in numbers if number not in citations)
    answer = '\n'.join(lines)
    raw = '\n'.join(item.text.strip() for item in value.statements)
    # 引用、标签和未知句是程序附加开销，单独计量，不挤占模型JSON生成额度。
    overhead = max(0, text_tokens(answer) - text_tokens(raw))
    if (type(max_output_tokens) is not int or max_output_tokens <= 0
            or text_tokens(raw) > max_output_tokens or text_tokens(answer) > max_output_tokens + overhead):
        raise ValueError('grounded_answer_render_limit_exceeded')
    return AskOutput(answer=answer, citations=citations)


def review_output_minimum(catalog, first_output, *, chosen, question):
    """按本轮全部去重位置计算输出外壳下界，不通过截掉任务伪装预算可用。"""
    from .policies import get
    from .policies.date_grounding import build_date_ref_catalog
    raw, _, diagnostics = inspect_referenced_first_draft(first_output, catalog, chosen=chosen,
        question=question, compose_version='@32')
    queue = get('compose', version='@32')(None, operation='review_queue', catalog=catalog,
        first_output=raw.model_dump(), diagnostics=diagnostics, date_refs=build_date_ref_catalog(catalog, chosen))
    return grounded_output_minimum(catalog, compose_version='@32', review_queue=queue)


def _revision_evidence_covers(evidence, unit, span, *, whole):
    import re
    """仅同单元support原引文具有采用资格；整行任务不宣称整行意义完整。"""
    if evidence.role != 'support' or evidence.unit_id != unit['id']:
        return False
    for match in re.finditer(re.escape(evidence.quote), unit['text']):
        if (match.start() <= span[0] and span[1] <= match.end()) if whole else (
                match.start() < span[1] and span[0] < match.end()):
            return True
    return False


def reviewed_reconstructed_answer(output, catalog, first_output, *, chosen, question):
    from .policies import get
    value = ReconstructedReviewedOutput.model_validate(output)
    raw, _, diagnostics = inspect_referenced_first_draft(first_output, catalog, chosen=chosen,
        question=question, compose_version='@32')
    from .policies.date_grounding import build_date_ref_catalog
    queue = get('compose', version='@32')(None, operation='review_queue', catalog=catalog,
        first_output=raw.model_dump(), diagnostics=diagnostics, date_refs=build_date_ref_catalog(catalog, chosen))
    items = {row['item_id']: row for row in queue['items']}
    units = {unit['id']: unit for unit in catalog['units']}
    seen = []
    for group in value.review_dispositions:
        if not group.reason.strip() or len(set(group.statement_indices)) != len(group.statement_indices):
            raise ValueError('review_disposition_invalid')
        seen.extend(group.item_ids)
        if group.action == 'exclude':
            if group.statement_indices:
                raise ValueError('review_exclusion_has_statement')
            continue
        if not group.statement_indices or any(index > len(value.statements) for index in group.statement_indices):
            raise ValueError('review_statement_position_invalid')
        for identity in group.item_ids:
            if identity not in items:
                raise ValueError('review_item_position_invalid')
            item = items[identity]
            if not any(_revision_evidence_covers(evidence, units[item['unit_id']], item['span'], whole=item['scope'] == 'span_review')
                    for index in group.statement_indices for evidence in value.statements[index - 1].evidence):
                raise ValueError('review_inclusion_span_not_supported')
        for index in group.statement_indices:
            if not any(_revision_evidence_covers(evidence, units[items[identity]['unit_id']], items[identity]['span'],
                    whole=items[identity]['scope'] == 'span_review')
                    for identity in group.item_ids for evidence in value.statements[index - 1].evidence):
                raise ValueError('review_inclusion_statement_not_supported')
    if len(set(seen)) != len(seen) or set(seen) != set(items):
        raise ValueError('review_item_coverage_invalid')
    render = get('compose', version='@32')(None, operation='stance_render', question=question)
    prefixes = render['prefixes']
    statements = []
    for statement in value.statements:
        clauses = []
        anchor_keys = {(anchor.unit_id, anchor.quote) for anchor in statement.source_clauses}
        if len(anchor_keys) != len(statement.source_clauses):
            raise ValueError('modal_source_clause_duplicate')
        anchored_spans = set()
        for anchor in statement.source_clauses:
            if anchor.unit_id not in units:
                raise ValueError('modal_source_clause_invalid')
            import re
            unit = units[anchor.unit_id]
            local = get('compose', version='@32')(None, operation='modal_clauses', text=unit['text'])
            starts, ends = {row['span'][0] for row in local}, {row['span'][1] for row in local}
            # 完整连续引文可覆盖多个相邻分句，避免名词并列被拆分后没有合法引用方式。
            occurrences = [match.span() for match in re.finditer(re.escape(anchor.quote), unit['text'])
                if match.start() in starts and match.end() in ends]
            valid = [span for span in occurrences if any(_revision_evidence_covers(evidence, unit, span, whole=True)
                for evidence in statement.evidence)]
            matches = [row for row in local if any(left <= row['span'][0] and row['span'][1] <= right for left, right in valid)]
            if not matches:
                raise ValueError('modal_source_clause_invalid')
            clauses.extend(matches)
            anchored_spans.update((anchor.unit_id, *row['span']) for row in matches)
        for evidence in statement.evidence:
            if evidence.role != 'support' or evidence.unit_id not in units:
                continue
            unit = units[evidence.unit_id]
            supported_clauses = [clause for clause in get('compose', version='@32')(
                None, operation='modal_clauses', text=unit['text']) if _revision_evidence_covers(evidence, unit, clause['span'], whole=True)]
            if not supported_clauses or not any((evidence.unit_id, *clause['span']) in anchored_spans for clause in supported_clauses):
                raise ValueError('modal_support_clause_unreviewed')
        if statement.kind != 'limitation' and not clauses:
            raise ValueError('modal_source_clause_missing')
        # 只核被声明实际引用的局部分句；其他单元或邻句中的未来词、问号没有权限。
        marked = {clause['stance'] for clause in clauses} - {'asserted'}
        if marked and (len(marked) != 1 or statement.stance not in marked | {'inference'}):
            raise ValueError('modal_source_stance_mismatch')
        if not marked and statement.stance in prefixes:
            raise ValueError('modal_source_stance_mismatch')
        if statement.stance == 'unknown' and statement.kind != 'limitation':
            raise ValueError('modal_unknown_kind_invalid')
        if statement.stance == 'inference' and statement.kind != 'inference':
            raise ValueError('modal_inference_kind_invalid')
        actual = statement.model_dump(exclude={'stance', 'source_clauses'})
        prefix = prefixes.get(statement.stance, '')
        if statement.stance == 'inference' and marked:
            # 推断与前提状态分开；实际公开文本必须保留前提仍为计划/求证，不能只改reason。
            premise = next(iter(marked))
            prefix = render['inference_premise'][premise]
        if prefix:
            actual['text'] = prefix + actual['text']
        # 比较实际公开字段，合法可见的模态限定和kind-only修复也属于真实修改。
        for use in statement.first_fact_uses:
            if use.action == 'correct' and 1 <= use.fact_index <= len(raw.facts):
                before = raw.facts[use.fact_index - 1].model_dump()
                if {key: actual[key] for key in before} == before:
                    raise ValueError('first_fact_replacement_unchanged')
        statements.append(actual)
    legacy = {**value.model_dump(exclude={'statements', 'review_dispositions'}), 'statements': statements}
    # 32未给日期算法增加权限；最终原字段仍交30/31同一严格出处与日历守卫。
    return reviewed_referenced_answer(legacy, catalog, first_output, chosen=chosen,
        question=question, compose_version='@31')
